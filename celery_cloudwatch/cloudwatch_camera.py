import sys
import traceback
import json
import logging

from camera import Camera
import boto.ec2.cloudwatch


logger = logging.getLogger(__name__)


class CloudWatchCamera(Camera):

    clear_after = True

    def __init__(self, state, config, aws_connection=None):
        super(CloudWatchCamera, self).__init__(state, config)
        self.verbose = config['camera']['verbose']
        if not config['cloudwatch-camera']['dryrun'] and aws_connection is None:
            aws_connection = boto.ec2.cloudwatch.CloudWatchConnection()
        self.aws_connection = aws_connection
        self.cloud_watch_namespace = config['cloudwatch-camera']['namespace']
        self.task_mapping = {}
        for task in config['cloudwatch-camera']['tasks']:
            if isinstance(task, dict):
                task_name = task['name']
                dimensions = task['dimensions']
            else:
                task_name = task
                dimensions = {'task': task}
            if task_name in self.task_mapping:
                logger.warn('Duplicate configuration for task %r', task)
            self.task_mapping[task_name] = dimensions
        self.metrics = None

    def on_shutter(self, state):
        try:
            self.metrics = self._build_metrics(state)
        except RuntimeError as r:
            print r

    def after_shutter(self):
        try:
            self.metrics.send()
        except:
            print "Exception in user code:"
            print '-'*60
            traceback.print_exc(file=sys.stdout)
        finally:
            self.metrics = None

    def _metric_list(self):
        return MetricList(self.cloud_watch_namespace, self.aws_connection, self.verbose)

    def _build_metrics(self, state):
        metrics = self._metric_list()
        self._add_queue_times(metrics, state.time_to_start)
        self._add_run_times(metrics, state.time_to_process)
        self._add_task_events(metrics,
                              state.task_event_waiting,
                              state.task_event_running,
                              state.task_event_completed,
                              state.task_event_failed,
                              state.num_waiting_by_task(),
                              state.num_running_by_task()
                              )
        return metrics

    def _add_task_events(self, metrics, task_event_waiting, task_event_running, task_event_completed, task_event_failed,
                         num_waiting_by_task, num_running_by_task):
        for task_name, dimensions in self.task_mapping.iteritems():
            metrics.add('CeleryTaskEventWaiting', unit='Count', value=task_event_waiting.get(task_name, 0), dimensions=dimensions)
            metrics.add('CeleryTaskEventRunning', unit='Count', value=task_event_running.get(task_name, 0), dimensions=dimensions)
            metrics.add('CeleryTaskEventCompleted', unit='Count', value=task_event_completed.get(task_name, 0), dimensions=dimensions)
            metrics.add('CeleryTaskEventFailed', unit='Count', value=task_event_failed.get(task_name, 0), dimensions=dimensions)
            metrics.add('CeleryTaskWaiting', unit='Count', value=num_waiting_by_task.get(task_name, 0), dimensions=dimensions)
            metrics.add('CeleryTaskRunning', unit='Count', value=num_running_by_task.get(task_name, 0), dimensions=dimensions)

    @staticmethod
    def _add_queue_times(metrics, time_to_start):
        for task_name, stats in time_to_start.items():
            metrics.add('CeleryTaskQueuedTime', unit='Seconds', dimensions={'task': task_name}, stats=stats.__dict__.copy())

    @staticmethod
    def _add_run_times(metrics, time_to_process):
        for task_name, stats in time_to_process.items():
            metrics.add('CeleryTaskProcessingTime', unit='Seconds', dimensions={'task': task_name}, stats=stats.__dict__.copy())

def xchunk(arr, size):
    for x in xrange(0, len(arr), size):
        yield arr[x:x+size]


class MetricList(object):

    _metric_chunk_size = 20

    def __init__(self, namespace, aws_connection, verbose=False):
        self.metrics = []
        self.namespace = namespace
        self.aws_connection = aws_connection
        self.verbose = verbose

    def add(self, *args, **kwargs):
        self.append(Metric(*args, **kwargs))

    def append(self, metric):
        self.metrics.append(metric)

    def _serialize(self, metric_chunk):
        params = {
            'Namespace': self.namespace
        }
        index = 0
        for metric in metric_chunk:
            for key, val in metric.serialize().iteritems():
                params['MetricData.member.%d.%s' % (index + 1, key)] = val
            index += 1
        return params

    def send(self):
        for metric_chunk in xchunk(self.metrics, self._metric_chunk_size):
            metrics = self._serialize(metric_chunk)
            if self.verbose:
                print 'PutMetricData'
                print json.dumps(metrics, indent=2, sort_keys=True)
            if self.aws_connection:
                self.aws_connection.get_status('PutMetricData', metrics, verb="POST")


class Metric(object):

    def __init__(self, name, unit=None, timestamp=None, value=None, stats=None, dimensions=None):
        self.name = name
        self.unit = unit
        self.timestamp = timestamp
        self.value = value
        self.stats = stats
        self.dimensions = dimensions

    def add_dimension(self, key, val):
        if self.dimensions is None or len(self.dimensions) == 0:
            self.dimensions = {}
        if key not in self.dimensions:
            self.dimensions[key] = val

    def serialize(self):
        metric_data = {
            'MetricName': self.name,
        }

        if self.timestamp:
            metric_data['Timestamp'] = self.timestamp.isoformat()

        if self.unit:
            metric_data['Unit'] = self.unit

        if self.dimensions:
            self._build_dimension_param(self.dimensions, metric_data)

        if self.stats:
            metric_data['StatisticValues.Maximum'] = self.stats['maximum']
            metric_data['StatisticValues.Minimum'] = self.stats['minimum']
            metric_data['StatisticValues.SampleCount'] = self.stats['samplecount']
            metric_data['StatisticValues.Sum'] = self.stats['sum']
        elif self.value is not None:
            metric_data['Value'] = self.value
        else:
            raise Exception('Must specify a value or statistics to put.')

        return metric_data

    @staticmethod
    def _build_dimension_param(dimensions, params):
        prefix = 'Dimensions.member'
        i = 0
        for dim_name in dimensions:
            dim_value = dimensions[dim_name]
            if dim_value:
                if isinstance(dim_value, basestring):
                    dim_value = [dim_value]
                for value in dim_value:
                    params['%s.%d.Name' % (prefix, i+1)] = dim_name
                    params['%s.%d.Value' % (prefix, i+1)] = value
                    i += 1
            else:
                params['%s.%d.Name' % (prefix, i+1)] = dim_name
                i += 1

    def __repr__(self):
        return '<Metric %s>' % self.name