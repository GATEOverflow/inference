"""
mlperf inference benchmarking tool
"""

from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import argparse
import json
import logging
import os
import threading
import time
from queue import Queue

import mlperf_loadgen as lg
import numpy as np

import dataset
import imagenet
import coco

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

NANO_SEC = 1000000000
MILLI_SEC = 1000

# pylint: disable=missing-docstring

# the datasets we support
SUPPORTED_DATASETS = {
    "imagenet":
        (imagenet.Imagenet, dataset.pre_process_vgg, dataset.PostProcessCommon(offset=-1),
         {"image_size": [224, 224, 3]}),
    "imagenet_mobilenet":
        (imagenet.Imagenet, dataset.pre_process_mobilenet, dataset.PostProcessArgMax(offset=-1),
         {"image_size": [224, 224, 3]}),
    "coco":
        (coco.Coco, dataset.pre_process_coco_mobilenet, coco.PostProcessCoco(),
         {"image_size": [-1, -1, 3]}),
    "coco-300":
        (coco.Coco, dataset.pre_process_coco_mobilenet, coco.PostProcessCoco(),
         {"image_size": [300, 300, 3]}),
    "coco-1200":
        (coco.Coco, dataset.pre_process_coco_resnet34, coco.PostProcessCoco(),
         {"image_size": [1200, 1200, 3]}),
}

# pre-defined command line options so simplify things. They are used as defaults and can be
# overwritten from command line
DEFAULT_LATENCY_BUCKETS = "0.010,0.050,0.100,0.200,0.400"

SUPPORTED_PROFILES = {
    "defaults": {
        "dataset": "imagenet",
        "backend": "tensorflow",
        "cache": 0,
        "time": 128,
        "max-latency": DEFAULT_LATENCY_BUCKETS,
    },

    # resnet
    "resnet50-tf": {
        "inputs": "input_tensor:0",
        "outputs": "ArgMax:0",
        "dataset": "imagenet",
        "backend": "tensorflow",
    },
    "resnet50-onnxruntime": {
        "dataset": "imagenet",
        "outputs": "ArgMax:0",
        "backend": "onnxruntime",
    },

    # mobilenet
    "mobilenet-tf": {
        "inputs": "input:0",
        "outputs": "MobilenetV1/Predictions/Reshape_1:0",
        "dataset": "imagenet_mobilenet",
        "backend": "tensorflow",
    },
    "mobilenet-onnxruntime": {
        "dataset": "imagenet_mobilenet",
        "outputs": "MobilenetV1/Predictions/Reshape_1:0",
        "backend": "onnxruntime",
    },

    # ssd-mobilenet
    "ssd-mobilenet-tf": {
        "inputs": "image_tensor:0",
        "outputs": "num_detections:0,detection_boxes:0,detection_scores:0,detection_classes:0",
        "dataset": "coco-300",
        "backend": "tensorflow",
    },
    "ssd-mobilenet-onnxruntime": {
        "dataset": "coco-300",
        "outputs": "num_detections:0,detection_boxes:0,detection_scores:0,detection_classes:0",
        "backend": "onnxruntime",
        "data-format": "NHWC",
    },

    # ssd-resnet34
    "ssd-resnet34-tf": {
        "inputs": "0:0",
        "outputs": "--work in progress--",
        "dataset": "coco-1200",
        "backend": "tensorflow",
    },
    "ssd-resnet34-onnxruntime": {
        "dataset": "coco-1200",
        "inputs": "image",
        "outputs": "bboxes,labels,scores",
        "backend": "onnxruntime",
        "data-format": "NCHW",
    },
}

last_timeing = []


def get_args():
    """Parse commandline."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", choices=SUPPORTED_DATASETS.keys(), help="dataset")
    parser.add_argument("--dataset-path", required=True, help="path to the dataset")
    parser.add_argument("--dataset-list", help="path to the dataset list")
    parser.add_argument("--data-format", choices=["NCHW", "NHWC"], help="data format")
    parser.add_argument("--profile", choices=SUPPORTED_PROFILES.keys(), help="standard profiles")
    parser.add_argument("--model", required=True, help="model file")
    parser.add_argument("--output", help="test results")
    parser.add_argument("--inputs", help="model inputs")
    parser.add_argument("--outputs", help="model outputs")
    parser.add_argument("--backend", help="runtime to use")
    parser.add_argument("--threads", default=os.cpu_count(), type=int, help="threads")
    parser.add_argument("--time", type=int, help="time to scan in seconds")
    parser.add_argument("--count", type=int, help="dataset items to use")
    parser.add_argument("--qps", type=int, default=10, help="target qps estimate")
    parser.add_argument("--max-latency", type=str, help="max latency in 99pct tile")
    parser.add_argument("--cache", type=int, default=0, help="use cache")
    parser.add_argument("--accuracy", action="store_true", help="enable accuracy pass")
    args = parser.parse_args()

    # don't use defaults in argparser. Instead we default to a dict, override that with a profile
    # and take this as default unless command line give
    defaults = SUPPORTED_PROFILES["defaults"]

    if args.profile:
        profile = SUPPORTED_PROFILES[args.profile]
        defaults.update(profile)
    for k, v in defaults.items():
        kc = k.replace("-", "_")
        if getattr(args, kc) is None:
            setattr(args, kc, v)
    if args.inputs:
        args.inputs = args.inputs.split(",")
    if args.outputs:
        args.outputs = args.outputs.split(",")
    if args.max_latency:
        args.max_latency = [float(i) for i in args.max_latency.split(",")]
    return args


def get_backend(backend):
    if backend == "tensorflow":
        from backend_tf import BackendTensorflow
        backend = BackendTensorflow()
    elif backend == "onnxruntime":
        from backend_onnxruntime import BackendOnnxruntime
        backend = BackendOnnxruntime()
    elif backend == "null":
        from backend_null import BackendNull
        backend = BackendNull()
    elif backend == "pytorch":
        from backend_pytorch import BackendPytorch
        backend = BackendPytorch()
    elif backend == "tflite":
        from backend_tflite import BackendTflite
        backend = BackendTflite()
    else:
        raise ValueError("unknown backend: " + backend)
    return backend


class Item:
    """An item that we queue for processing by the thread pool."""

    def __init__(self, query_id, content_id, img, label=None):
        self.query_id = query_id
        self.content_id = content_id
        self.img = img
        self.label = label
        self.start = time.time()


class Runner:
    def __init__(self, model, ds, threads, post_proc=None):
        self.tasks = Queue(maxsize=threads * 5)
        self.workers = []
        self.model = model
        self.post_process = post_proc
        self.threads = threads
        self.result_dict = {}
        self.take_accuracy = False
        self.ds = ds

    def handle_tasks(self, tasks_queue):
        """Worker thread."""
        while True:
            qitem = tasks_queue.get()
            if qitem is None:
                # None in the queue indicates the parent want us to exit
                tasks_queue.task_done()
                break

            try:
                # run the prediction
                results = self.model.predict({self.model.inputs[0]: qitem.img})
                if self.take_accuracy:
                    response = self.post_process(results, qitem.content_id, qitem.label, self.result_dict)
            except Exception as ex:  # pylint: disable=broad-except
                src = [self.ds.get_item_loc(i) for i in qitem.content_id]
                log.error("thread: failed on contentid=%s, %s", src, ex)
            finally:
                response = []
                for query_id in qitem.query_id:
                    # FIXME: unclear what to return here
                    response.append(lg.QuerySampleResponse(query_id, 0, 0))
                lg.QuerySamplesComplete(response)
            tasks_queue.task_done()

    def handle_tasks_nolg(self, tasks_queue):
        """Worker thread."""
        while True:
            qitem = tasks_queue.get()
            if qitem is None:
                # None in the queue indicates the parent want us to exit
                tasks_queue.task_done()
                break

            try:
                # run the prediction
                start = time.time()
                results = self.model.predict({self.model.inputs[0]: qitem.img})
                self.result_dict["timing"].append(time.time() - start)
                if self.take_accuracy:
                    response = self.post_process(results, qitem.content_id, qitem.label, self.result_dict)
            except Exception as ex:  # pylint: disable=broad-except
                src = [self.ds.get_item_loc(i) for i in qitem.content_id]
                log.error("thread: failed on contentid=%s, %s", src, ex)
            finally:
                tasks_queue.task_done()

    def start_pool(self, nolg=False):
        if nolg:
            handler =self.handle_tasks_nolg
        else:
            handler =self.handle_tasks
        for _ in range(self.threads):
            worker = threading.Thread(target=handler, args=(self.tasks,))
            worker.daemon = True
            self.workers.append(worker)
            worker.start()

    def start_run(self, result_dict, take_accuracy):
        self.result_dict = result_dict
        self.take_accuracy = take_accuracy
        self.post_process.start()

    def enqueue(self, id, ids, data, label):
        item = Item(id, ids, data, label)
        self.tasks.put(item)

    def finish(self):
        # exit all threads
        for _ in self.workers:
            self.tasks.put(None)
        for worker in self.workers:
            worker.join()


def add_results(final_results, name, result_dict, result_list, took):
    percentiles = [50., 80., 90., 95., 99., 99.9]
    buckets = np.percentile(result_list, percentiles).tolist()
    buckets_str = ",".join(["{}:{:.4f}".format(p, b) for p, b in zip(percentiles, buckets)])

    if result_dict["total"] == 0:
        result_dict["total"] = len(result_list)

    # this is what we record for each run
    result = {
        "mean": np.mean(result_list),
        "took": took,
        "qps": len(result_list) / took,
        "count": len(result_list),
        "percentiles": {str(k): v for k, v in zip(percentiles, buckets)},
        "good_items": result_dict["good"],
        "total_items": result_dict["total"],
        "accuracy": 100. * result_dict["good"] / result_dict["total"],
    }
    mAP = ""
    if "mAP" in result_dict:
        result["mAP"] = result_dict["mAP"]
        mAP = ", mAP={:.2f}".format(result_dict["mAP"])

    # add the result to the result dict
    final_results[name] = result

    # to stdout
    print("{} qps={:.2f}, mean={:.6f}, time={:.2f}, acc={:.2f}{}, queries={}, tiles={}".format(
        name, result["qps"], result["mean"], took, result["accuracy"], mAP,
        len(result_list), buckets_str))


def main():
    global last_timeing
    args = get_args()

    log.info(args)

    # find backend
    backend = get_backend(args.backend)

    # override image format if given
    image_format = args.data_format if args.data_format else backend.image_format()

    # dataset to use
    wanted_dataset, pre_proc, post_proc, kwargs = SUPPORTED_DATASETS[args.dataset]
    ds = wanted_dataset(data_path=args.dataset_path,
                        image_list=args.dataset_list,
                        name=args.dataset,
                        image_format=image_format,
                        pre_process=pre_proc,
                        use_cache=args.cache,
                        count=args.count, **kwargs)

    # load model to backend
    model = backend.load(args.model, inputs=args.inputs, outputs=args.outputs)

    final_results = {
        "runtime": model.name(),
        "version": model.version(),
        "time": int(time.time()),
        "cmdline": str(args),
    }

    #
    # make one pass over the dataset to validate accuracy
    #
    count = args.count if args.count else ds.get_item_count()

    runner = Runner(model, ds, args.threads, post_proc=post_proc)

    #
    # warmup
    #
    log.info("warmup ...")
    ds.load_query_samples([0])
    for _ in range(50):
        img, _ = ds.get_samples([0])
        _ = backend.predict({backend.inputs[0]: img})
    ds.unload_query_samples(None)

    #
    # accuracy pass
    #
    if args.accuracy:
        log.info("starting accuracy pass on {} items".format(count))
        runner.start_pool(nolg=True)
        result_dict = {"good": 0, "total": 0, "scenario": "Accuracy", "timing": []}
        runner.start_run(result_dict, True)
        start = time.time()
        for idx in range(0, count):
            ds.load_query_samples([idx])
            data, label = ds.get_samples([idx])
            runner.enqueue([idx], [idx], data, label)
        runner.finish()
        # aggregate results
        post_proc.finalize(result_dict, ds, output_dir=os.path.dirname(args.output))
        last_timeing = result_dict["timing"]
        del result_dict["timing"]
        add_results(final_results, "Accuracy", result_dict, last_timeing, time.time() - start)

    #
    # run the benchmark with timeing
    #
    runner.start_pool()

    def issue_query(query_samples):
        idx = [q.index for q in query_samples]
        query_id = [q.id for q in query_samples]
        data, label = ds.get_samples(idx)
        runner.enqueue(query_id, idx, data, label)

    def process_latencies(latencies_ns):
        global last_timeing
        last_timeing = [t / 10000000. for t in latencies_ns]

    sut = lg.ConstructSUT(issue_query, process_latencies)
    qsl = lg.ConstructQSL(count, min(count, 1000), ds.load_query_samples, ds.unload_query_samples)

    scenarios = [
        lg.TestScenario.SingleStream,
        lg.TestScenario.MultiStream,
        lg.TestScenario.Server,
        # lg.TestScenario.Offline,
        ]

    for scenario in scenarios:
        for target_latency in args.max_latency:
            log.info("starting {}, latency={}".format(scenario, target_latency))
            settings = lg.TestSettings()
            settings.scenario = scenario

            if args.qps:
                settings.enable_spec_overrides = True
                qps = float(args.qps)
                settings.server_target_qps = qps
                settings.offline_expected_qps = qps

            if args.time:
                settings.enable_spec_overrides = True
                settings.override_min_duration_ms = args.time * MILLI_SEC
                settings.override_max_duration_ms = args.time * MILLI_SEC
                qps = args.qps or 100
                settings.override_min_query_count = qps * args.time
                settings.override_max_query_count = qps * args.time

            if args.time or args.qps:
                settings.mode = lg.TestMode.PerformanceOnly
            # FIXME: add SubmissionRun once available

            settings.enable_spec_overrides = True
            settings.single_stream_expected_latency_ns = int(target_latency * NANO_SEC)
            settings.override_target_latency_ns = int(target_latency * NANO_SEC)

            result_dict = {"good": 0, "total": 0, "scenario": str(scenario)}
            runner.start_run(result_dict, False)
            lg.StartTest(sut, qsl, settings)

            add_results(final_results, "{}-{}".format(scenario, target_latency),
                        result_dict, last_timeing, time.time() - ds.last_loaded)

    #
    # write final results
    #
    if args.output:
        with open(args.output, "w") as f:
            json.dump(final_results, f, sort_keys=True, indent=4)

    runner.finish()
    lg.DestroyQSL(qsl)
    lg.DestroySUT(sut)


if __name__ == "__main__":
    main()
