"""Unit tests for the standalone reefy-metrics-publisher script."""

import importlib.machinery
import importlib.util
import os
import sys
import types
import unittest
from io import StringIO
from unittest import mock


PUBLISHER_PATH = os.path.join(os.path.dirname(__file__), '..',
                              'rootfs-overlay', 'usr', 'bin',
                              'reefy-metrics-publisher')


def _load_publisher():
    fake_paho = types.ModuleType('paho')
    fake_mqtt_pkg = types.ModuleType('paho.mqtt')
    fake_client = types.ModuleType('paho.mqtt.client')
    fake_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    fake_client.Client = object

    old_modules = {
        name: sys.modules.get(name)
        for name in ('paho', 'paho.mqtt', 'paho.mqtt.client')
    }
    sys.modules['paho'] = fake_paho
    sys.modules['paho.mqtt'] = fake_mqtt_pkg
    sys.modules['paho.mqtt.client'] = fake_client
    try:
        loader = importlib.machinery.SourceFileLoader(
            'reefy_metrics_publisher_test', PUBLISHER_PATH)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        for name, old in old_modules.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


class NvidiaGpuTests(unittest.TestCase):
    def setUp(self):
        self.publisher = _load_publisher()

    def test_nvidia_power_draw_is_published(self):
        stdout = (
            '0, NVIDIA RTX 6000, 00000000:01:00.0, GPU-abc, '
            '73, 2048, 24576, 64, 187.42\n'
        )
        with mock.patch.object(self.publisher.shutil, 'which',
                               return_value='/usr/bin/nvidia-smi'), \
             mock.patch.object(self.publisher.subprocess, 'run',
                               return_value=types.SimpleNamespace(
                                   returncode=0, stdout=stdout)):
            samples = self.publisher._collect_nvidia_gpu()

        by_name = {s['name']: s for s in samples}
        self.assertEqual(by_name['reefy_gpu_power_watts']['value'], 187.42)
        self.assertEqual(
            by_name['reefy_gpu_power_watts']['labels']['driver'], 'nvidia')
        self.assertEqual(by_name['reefy_gpu_util_pct']['value'], 73.0)

    def test_nvidia_na_power_does_not_drop_other_metrics(self):
        stdout = (
            '0, NVIDIA RTX 6000, 00000000:01:00.0, GPU-abc, '
            '73, 2048, 24576, 64, N/A\n'
        )
        with mock.patch.object(self.publisher.shutil, 'which',
                               return_value='/usr/bin/nvidia-smi'), \
             mock.patch.object(self.publisher.subprocess, 'run',
                               return_value=types.SimpleNamespace(
                                   returncode=0, stdout=stdout)):
            samples = self.publisher._collect_nvidia_gpu()

        names = {s['name'] for s in samples}
        self.assertIn('reefy_gpu_util_pct', names)
        self.assertIn('reefy_gpu_mem_used_pct', names)
        self.assertIn('reefy_gpu_temp_celsius', names)
        self.assertNotIn('reefy_gpu_power_watts', names)


class IntelXeGpuTests(unittest.TestCase):
    def setUp(self):
        self.publisher = _load_publisher()

    def test_gt_idle_delta_emits_per_gt_and_aggregate_util(self):
        self.publisher._prev_xe_gt_idle = {
            '/sys/class/drm/card0:tile0:gt0': (1000, 100.0),
            '/sys/class/drm/card0:tile0:gt1': (2000, 100.0),
        }

        def fake_listdir(path):
            if path == '/sys/class/drm/card0/device':
                return ['tile0']
            if path == '/sys/class/drm/card0/device/tile0':
                return ['gt0', 'gt1']
            return []

        def fake_read_int(path):
            if '/gt0/' in path:
                return 31000
            if '/gt1/' in path:
                return 47000
            return None

        labels = {'gpu': '0', 'name': 'Intel GPU', 'driver': 'xe'}
        with mock.patch.object(self.publisher.time, 'time',
                               return_value=160.0), \
             mock.patch.object(self.publisher.os, 'listdir',
                               side_effect=fake_listdir), \
             mock.patch.object(self.publisher.os.path, 'isdir',
                               return_value=True), \
             mock.patch.object(self.publisher, '_read_int',
                               side_effect=fake_read_int):
            samples = self.publisher._collect_xe_gt_util(
                '/sys/class/drm/card0', labels)

        engine = {
            s['labels']['gt']: s['value']
            for s in samples
            if s['name'] == 'reefy_gpu_engine_util_pct'
        }
        aggregate = [
            s['value'] for s in samples
            if s['name'] == 'reefy_gpu_util_pct'
        ]
        self.assertAlmostEqual(engine['tile0/gt0'], 50.0)
        self.assertAlmostEqual(engine['tile0/gt1'], 25.0)
        self.assertEqual(aggregate, [50.0])

    def test_gtt_mm_page_count_total_is_converted_to_bytes(self):
        labels = {'gpu': '0', 'name': 'Intel GPU', 'driver': 'xe'}
        gtt_mm = '  use_type: 1\n  size: 4011285\n  usage: 1977053184\n'

        def fake_open(path, *args, **kwargs):
            if path.endswith('/gtt_mm'):
                return StringIO(gtt_mm)
            raise OSError(path)

        with mock.patch.object(self.publisher, '_path_exists',
                               return_value=True), \
             mock.patch.object(self.publisher, '_pci_id',
                               return_value='0000:00:02.0'), \
             mock.patch.object(self.publisher.os, 'sysconf',
                               return_value=4096), \
             mock.patch('builtins.open', side_effect=fake_open):
            samples = self.publisher._collect_xe_debug_mem(
                '/sys/class/drm/card0', labels)

        by_name = {s['name']: s for s in samples}
        self.assertEqual(
            by_name['reefy_gpu_mem_total_bytes']['value'],
            4011285 * 4096)
        self.assertEqual(
            by_name['reefy_gpu_mem_used_bytes']['value'],
            1977053184.0)
        self.assertAlmostEqual(
            by_name['reefy_gpu_mem_used_pct']['value'],
            1977053184.0 / (4011285 * 4096) * 100)

    def test_rapl_uncore_power_emits_on_second_sample(self):
        self.publisher._prev_xe_energy = {
            '/sys/class/powercap/intel-rapl:0:1/energy_uj': (1000000.0, 10.0)
        }

        def fake_listdir(path):
            if path == '/sys/class/powercap':
                return ['intel-rapl:0:1']
            return []

        def fake_open(path, *args, **kwargs):
            if path.endswith('/name'):
                return StringIO('uncore\n')
            raise OSError(path)

        def fake_read_float(path):
            if path.endswith('/energy_uj'):
                return 16000000.0
            return None

        labels = {'gpu': '0', 'name': 'Intel GPU', 'driver': 'xe'}
        with mock.patch.object(self.publisher.time, 'time',
                               return_value=20.0), \
             mock.patch.object(self.publisher.os.path, 'isdir',
                               return_value=True), \
             mock.patch.object(self.publisher.os, 'listdir',
                               side_effect=fake_listdir), \
             mock.patch.object(self.publisher, '_read_float',
                               side_effect=fake_read_float), \
             mock.patch('builtins.open', side_effect=fake_open):
            samples = self.publisher._collect_xe_rapl_power(labels)

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]['name'], 'reefy_gpu_power_watts')
        self.assertEqual(samples[0]['labels']['power_source'], 'rapl_uncore')
        self.assertAlmostEqual(samples[0]['value'], 1.5)

    def test_thermal_zone_fallback_prefers_tcpu_pci(self):
        def fake_listdir(path):
            if path == '/sys/class/thermal':
                return ['thermal_zone0', 'thermal_zone1', 'thermal_zone2']
            return []

        def fake_open(path, *args, **kwargs):
            if path.endswith('/thermal_zone0/type'):
                return StringIO('x86_pkg_temp\n')
            if path.endswith('/thermal_zone1/type'):
                return StringIO('TCPU_PCI\n')
            if path.endswith('/thermal_zone2/type'):
                return StringIO('acpitz\n')
            raise OSError(path)

        def fake_read_float(path):
            if path.endswith('/thermal_zone0/temp'):
                return 66000.0
            if path.endswith('/thermal_zone1/temp'):
                return 65000.0
            return None

        labels = {'gpu': '0', 'name': 'Intel GPU', 'driver': 'xe'}
        with mock.patch.object(self.publisher.os.path, 'isdir',
                               return_value=True), \
             mock.patch.object(self.publisher.os, 'listdir',
                               side_effect=fake_listdir), \
             mock.patch.object(self.publisher, '_read_float',
                               side_effect=fake_read_float), \
             mock.patch('builtins.open', side_effect=fake_open):
            samples = self.publisher._collect_xe_thermal_zone_temp(labels)

        self.assertEqual(samples, [{
            'name': 'reefy_gpu_temp_celsius',
            'labels': {
                'gpu': '0',
                'name': 'Intel GPU',
                'driver': 'xe',
                'temp_source': 'thermal_zone:TCPU_PCI',
            },
            'value': 65.0,
        }])


class MixedGpuIndexTests(unittest.TestCase):
    def setUp(self):
        self.publisher = _load_publisher()

    def test_next_gpu_index_continues_after_nvidia_samples(self):
        samples = [
            {'name': 'reefy_gpu_util_pct', 'labels': {'gpu': '0'}},
            {'name': 'reefy_gpu_power_watts', 'labels': {'gpu': '2'}},
            {'name': 'reefy_cpu_pct', 'value': 50},
        ]
        self.assertEqual(self.publisher._next_gpu_index(samples), 3)


if __name__ == '__main__':
    unittest.main()
