import os

import logging

import shutil
import datetime
import sys
import csv

import queue
import threading
import concurrent.futures
import multiprocessing

# import multiprocessing.dummy as multiprocessing
import tensorflow.compat.v2 as tf


def init_mp(tf2=True):
    if tf2:
        multiprocessing.set_start_method("spawn")


def copy_source(file, output_dir):
    with tf.io.gfile.GFile(
        os.path.join(output_dir, os.path.basename(file)), mode="wb"
    ) as f:
        with tf.io.gfile.GFile(file, mode="rb") as f0:
            shutil.copyfileobj(f0, f)


from logging import StreamHandler, getLevelName, Handler


class FileHandler(StreamHandler):
    """
    A handler class which writes formatted logging records to disk files.
    """

    def __init__(self, filename, mode="a", encoding=None, delay=False):
        """
        Open the specified file and use it as the stream for logging.
        """
        # Issue #27493: add support for Path objects to be passed in
        # keep the absolute path, otherwise derived classes which use this
        # may come a cropper when the current directory changes
        self.baseFilename = os.path.abspath(filename)
        self.mode = mode
        self.encoding = encoding
        self.delay = delay
        if delay:
            # We don't open the stream, but we still need to call the
            # Handler constructor to set level, formatter, lock etc.
            Handler.__init__(self)
            self.stream = None
        else:
            StreamHandler.__init__(self, self._open())
        with tf.io.gfile.GFile(self.baseFilename, "w") as f:
            f.write("Logging ........\n")

    def close(self):
        """
        Closes the stream.
        """
        self.acquire()
        try:
            try:
                if self.stream:
                    try:
                        self.flush()
                    finally:
                        stream = self.stream
                        self.stream = None
                        if hasattr(stream, "close"):
                            stream.close()
            finally:
                # Issue #19523: call unconditionally to
                # prevent a handler leak when delay is set
                StreamHandler.close(self)
        finally:
            self.release()

    def _open(self):
        """
        Open the current base file with the (original) mode and encoding.
        Return the resulting stream.
        """
        return tf.io.gfile.GFile(self.baseFilename, self.mode)

    def emit(self, record):
        """
        Emit a record.

        If the stream was not opened because 'delay' was specified in the
        constructor, open it before calling the superclass's emit.
        """
        if self.stream is None:
            self.stream = self._open()
        StreamHandler.emit(self, record)

    def __repr__(self):
        level = getLevelName(self.level)
        return "<%s %s (%s)>" % (self.__class__.__name__, self.baseFilename, level)


def setup_logging_file(name, f, console=True):
    log_format = logging.Formatter("%(asctime)s : %(message)s")
    logger = logging.getLogger(name)
    logger.handlers = []
    file_handler = FileHandler(f)
    file_handler.setFormatter(log_format)
    logger.addHandler(file_handler)
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(log_format)
        logger.addHandler(console_handler)
    logger.setLevel(logging.INFO)
    return logger


def setup_logging(name, output_dir, console=True):
    log_format = logging.Formatter("%(asctime)s : %(message)s")
    logger = logging.getLogger(name)
    logger.handlers = []
    output_file = os.path.join(output_dir, "output.log")
    file_handler = FileHandler(output_file)
    file_handler.setFormatter(log_format)
    logger.addHandler(file_handler)
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(log_format)
        logger.addHandler(console_handler)
    logger.setLevel(logging.INFO)
    return logger


def get_argv():
    argv = sys.argv
    for i in range(1, len(argv)):
        if argv[i] == "--ckpt_load":
            argv.pop(i)
            argv.pop(i)
            break
    for i in range(1, len(argv)):
        if argv[i].startswith("--ckpt_load="):
            argv.pop(i)
            break
    for i in range(1, len(argv)):
        if argv[i] == "--device":
            argv.pop(i)
            argv.pop(i)
            break
    for i in range(1, len(argv)):
        if argv[i].startswith("--device="):
            argv.pop(i)
            break
    return "".join(argv[1:])


def get_output_filename(file):
    file_name = get_exp_id(file)
    if len(sys.argv) > 1:
        file_name = file_name + get_argv()
    return file_name


def get_output_dir(exp_id, rootdir):
    t = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    output_dir = os.path.join(rootdir, "output/" + exp_id, t)
    if len(sys.argv) > 1:
        output_dir = output_dir + get_argv()
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    return output_dir


free_devices_lock = threading.Lock()
free_devices = queue.Queue()


def fill_queue(device_ids):
    [free_devices.put_nowait(device_id) for device_id in device_ids]


def allocate_device():
    try:
        free_devices_lock.acquire()
        return free_devices.get()
    finally:
        free_devices_lock.release()


def free_device(device):
    try:
        free_devices_lock.acquire()
        return free_devices.put_nowait(device)
    finally:
        free_devices_lock.release()


job_file_lock = threading.Lock()


def update_job_status(job_id, job_status, read_opts, write_opts):
    try:
        job_file_lock.acquire()

        opts = read_opts()
        opt = next(opt for opt in opts if opt["job_id"] == job_id)
        opt["status"] = job_status
        write_opts(opts)
    except Exception:
        logging.exception("exception in update_job_status()")
    finally:
        job_file_lock.release()


def update_job_result_file(
    update_job_result, job_opt, job_stats, read_opts, write_opts
):
    try:
        job_file_lock.acquire()

        opts = read_opts()
        target_opt = next(opt for opt in opts if opt["job_id"] == job_opt["job_id"])
        update_job_result(target_opt, job_stats)

        write_opts(opts)
    finally:
        job_file_lock.release()


run_job_lock = threading.Lock()


def run_job(logger, opt, output_dir, output_dir_ckpt, train):
    device_id = allocate_device()
    opt_override = {"device": device_id}

    def merge(a, b):
        d = {}
        d.update(a)
        d.update(b)
        return d

    # opt = {**opt, **opt_override}
    opt = merge(opt, opt_override)
    logger.info("new job: job_id={}, device_id={}".format(opt["job_id"], opt["device"]))
    try:
        logger.info(
            "spawning process: job_id={}, device_id={}".format(
                opt["job_id"], opt["device"]
            )
        )

        try:
            output_dir_thread = os.path.join(output_dir, str(opt["job_id"]))
            os.makedirs(output_dir_thread, exist_ok=True)

            output_dir_thread_ckpt = os.path.join(output_dir_ckpt, str(opt["job_id"]))
            os.makedirs(output_dir_thread_ckpt, exist_ok=True)

            # logger_thread = setup_logging('job{}'.format(opt['job_id']), output_dir_thread, console=True)

            run_job_lock.acquire()
            manager = multiprocessing.Manager()
            return_dict = manager.dict()
            p = multiprocessing.Process(
                target=train,
                args=(
                    opt,
                    output_dir,
                    output_dir_thread,
                    output_dir_thread_ckpt,
                    return_dict,
                ),
            )
            p.start()

            # return_dict = {}
            # train(opt, output_dir_thread, logger_thread, return_dict)
        finally:
            run_job_lock.release()

        p.join()

        logger.info(
            "finished process: job_id={}, device_id={}".format(
                opt["job_id"], opt["device"]
            )
        )

        return return_dict["stats"]
    finally:
        free_device(device_id)


def run_jobs(
    logger,
    exp_id,
    output_dir,
    output_dir_ckpt,
    workers,
    train_job,
    read_opts,
    write_opts,
    update_job_result,
):
    opt_list = read_opts()
    opt_open = [opt for opt in opt_list if opt["status"] == "open"]
    logger.info(
        "scheduling {} open of {} total jobs".format(len(opt_open), len(opt_list))
    )
    logger.info("starting thread pool with {} workers".format(workers))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:

        def adjust_opt(opt):
            opt_override = {"exp_id": "{}_{}".format(exp_id, opt["job_id"])}

            def merge(a, b):
                d = {}
                d.update(a)
                d.update(b)
                return d

            # return {**opt, **opt_override}
            return merge(opt, opt_override)

        def do_run_job(opt):
            update_job_status(opt["job_id"], "running", read_opts, write_opts)
            return run_job(
                logger, adjust_opt(opt), output_dir, output_dir_ckpt, train_job
            )

        futures = {executor.submit(do_run_job, opt): opt for opt in opt_open}
        # do_run_job(opt_open[0])
        # futures = []

        for future in concurrent.futures.as_completed(futures):
            opt = futures[future]
            try:
                stats = future.result()
                logger.info("finished job future: job_id={}".format(opt["job_id"]))
                update_job_result_file(
                    update_job_result, opt, stats, read_opts, write_opts
                )
                update_job_status(opt["job_id"], "finished", read_opts, write_opts)
            except Exception:
                logger.exception("exception in run_jobs()")
                update_job_status(opt["job_id"], "fail", read_opts, write_opts)


def is_int(value):
    try:
        int(value)
        return True
    except ValueError:
        return False


def is_float(value):
    try:
        float(value)
        return not is_int(value)
    except ValueError:
        return False


def is_bool(value):
    return value.upper() in ["TRUE", "FALSE"]


def is_array(value):
    return "[" in value


def cast_str(value):
    if is_int(value):
        return int(value)
    if is_float(value):
        return float(value)
    if is_bool(value):
        return value.upper() == "TRUE"
    if is_array(value):
        return eval(value)
    return value


def get_exp_id(file):
    return os.path.splitext(os.path.basename(file))[0]


def overwrite_opt(opt, opt_override):
    for (k, v) in opt_override.items():
        setattr(opt, k, v)
    return opt


def write_opts(opt_list, f):
    writer = csv.writer(f(), delimiter=",")
    header = [key for key in opt_list[0]]
    writer.writerow(header)
    for opt in opt_list:
        writer.writerow([opt[k] for k in header])


def read_opts(f):
    opt_list = []
    reader = csv.reader(f(), delimiter=",")
    header = next(reader)
    for values in reader:
        opt = {}
        for i, field in enumerate(header):
            opt[field] = cast_str(values[i])
        opt_list += [opt]
    return opt_list


def reset_job_status(opts_list):
    for opt in opts_list:
        if opt["status"] == "running":
            opt["status"] = "open"
    return opts_list
