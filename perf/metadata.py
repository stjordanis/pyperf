from __future__ import print_function
import datetime
import os
import platform
import re
import socket
import subprocess
import sys
import time

import six
try:
    # Optional dependency
    import psutil
except ImportError:
    psutil = None

import perf


def _collect_python_metadata(metadata):
    # Implementation
    if hasattr(sys, 'implementation'):
        # PEP 421, Python 3.3
        metadata['python_implementation'] = sys.implementation.name
    else:
        # Convert to lower case to use the same format than Python 3
        python_impl = platform.python_implementation().lower()
        metadata['python_implementation'] = python_impl

    version = platform.python_version()
    bits = platform.architecture()[0]
    if bits:
        version = '%s (%s)' % (version, bits)
    metadata['python_version'] = version
    if sys.executable:
        metadata['python_executable'] = sys.executable

    # Before PEP 393 (Python 3.3)
    if sys.version_info < (3, 3):
        if sys.maxunicode == 0xffff:
            unicode_impl = 'UTF-16'
        else:
            unicode_impl = 'UCS-4'
        metadata['python_unicode'] = unicode_impl

    # timer
    if (hasattr(time, 'perf_counter')
       and perf.perf_counter == time.perf_counter):

        info = time.get_clock_info('perf_counter')
        metadata['timer'] = ('%s, resolution: %s'
                             % (info.implementation,
                                perf._format_timedelta(info.resolution)))
    elif perf.perf_counter == time.clock:
        metadata['timer'] = 'time.clock()'
    elif perf.perf_counter == time.time:
        metadata['timer'] = 'time.time()'


def _open_text(path):
    if six.PY3:
        return open(path, encoding="utf-8")
    else:
        return open(path)


def _first_line(path, default=None):
    try:
        fp = _open_text(path)
        try:
            line = fp.readline()
        finally:
            fp.close()
        return line.rstrip()
    except IOError:
        if default is not None:
            return default
        raise

def _read_proc(path):
    path = os.path.join('/proc', path)
    try:
        fp = _open_text(path)
        try:
            for line in fp:
                yield line.rstrip()
        finally:
            fp.close()
    except (OSError, IOError):
        return


def _sys_path(path):
    return os.path.join("/sys", path)


def _collect_linux_metadata(metadata):
    # CPU model
    for line in _read_proc("cpuinfo"):
        if line.startswith('model name'):
            model_name = line.split(':', 1)[1].strip()
            if model_name:
                metadata['cpu_model_name'] = model_name
            break

    # ASLR
    for line in _read_proc('sys/kernel/randomize_va_space'):
        if line == '0':
            metadata['aslr'] = 'No randomization'
        elif line == '1':
            metadata['aslr'] = 'Conservative randomization'
        elif line == '2':
            metadata['aslr'] = 'Full randomization'
        break


def _get_cpu_affinity():
    if hasattr(os, 'sched_getaffinity'):
        return os.sched_getaffinity(0)

    if psutil is not None:
        proc = psutil.Process()
        # cpu_affinity() is only available on Linux, Windows and FreeBSD
        if hasattr(proc, 'cpu_affinity'):
            return proc.cpu_affinity()

    return None


def _get_logical_cpu_count():
    if psutil is not None:
        # Number of logical CPUs
        cpu_count = psutil.cpu_count()
    elif hasattr(os, 'cpu_count'):
        # Python 3.4
        cpu_count = os.cpu_count()
    else:
        cpu_count = None
        try:
            import multiprocessing
        except ImportError:
            pass
        else:
            try:
                cpu_count = multiprocessing.cpu_count()
            except NotImplementedError:
                pass
    if cpu_count is not None and cpu_count < 1:
        return None
    return cpu_count


def _collect_system_metadata(metadata):
    metadata['platform'] = platform.platform(True, False)
    if sys.platform.startswith('linux'):
        _collect_linux_metadata(metadata)

    # on linux, load average over 1 minute
    for line in _read_proc("loadavg"):
        loadavg = line.split()[0]
        metadata['load_avg_1min'] = float(loadavg)

    # Hostname
    hostname = socket.gethostname()
    if hostname:
        metadata['hostname'] = hostname


def _collect_memory_metadata(metadata):
    if psutil is None:
        for line in _read_proc('/proc/self/status'):
            if line.startswith('VmRSS:') and line.endswith(' kB'):
                line = line[6:-3].strip()
                rss_kb = int(line)
                metadata['mem_rss'] = rss_kb * 1024
                break
        return

    # get rss memory
    process = psutil.Process()
    mem_info = process.memory_info()
    metadata['mem_rss'] = mem_info.rss

    # FIXME: support FreeBSD and Windows
    if sys.platform.startswith('linux'):
        # get private memory
        private = 0
        for mem_map in process.memory_maps():
            private += mem_map.private_clean + mem_map.private_dirty
        metadata['mem_private'] = private


def _get_cpu_boost(cpu):
    if not _get_cpu_boost.working:
        return

    env = dict(os.environ, LC_ALL='C')
    args = ['cpupower', '-c', str(cpu), 'frequency-info']
    try:
        proc = subprocess.Popen(args,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                universal_newlines=True,
                                env=env)
        stdout = proc.communicate()[0]
        if proc.returncode != 0:
            # if the command failed once, never try it again
            # (consider that the command is not installed or does not work)
            _get_cpu_boost.working = False
            return None
    except OSError:
        _get_cpu_boost.working = False
        return None

    boost = False
    for line in stdout.splitlines():
        if boost:
            if 'Supported:' in line:
                value = line.split(':', 1)[-1].strip()
                if value == 'no':
                    return False
                if value == 'yes':
                    return True
                raise ValueError("unable to parse: %r" % line)
        elif 'boost state support' in line:
            boost = True

    raise ValueError("unable to parse cpupower output: %r" % stdout)
_get_cpu_boost.working = True


def _format_cpu_infos(infos, cpus):
    if len(infos) == len(cpus):
        merge = (len(set(infos[cpu] for cpu in cpus)) == 1)
    else:
        merge = False
    if not merge:
        text = []
        for cpu in cpus:
            info = infos[cpu]
            text.append('%s=%s' % (cpu, info))
        text = ', '.join(text)
    else:
        # compact output if all CPUs have the same info
        cpu = list(cpus)[0]
        info = infos[cpu]
        cpus = perf._format_cpu_list(cpus)
        text = '%s=%s' % (cpus, info)
    return text


def _collect_cpu_freq(metadata, cpus):
    sys_path = _sys_path("devices/system/cpu")

    cpus = set(cpus)
    cpu_freq = {}
    cpu = None
    for line in _read_proc('cpuinfo'):
        if line.startswith('processor'):
            value = line.split(':', 1)[-1].strip()
            cpu = int(value)
            if cpu not in cpus:
                # skip this CPU
                cpu = None
        elif line.startswith('cpu MHz') and cpu is not None:
            mhz = line.split(':', 1)[-1].strip()
            mhz = float(mhz)
            mhz = int(round(mhz))
            cpu_freq[cpu] = '%s MHz' % mhz

    if not cpu_freq:
        return

    metadata['cpu_freq'] = _format_cpu_infos(cpu_freq, cpus)


def _get_cpu_config(cpu):
    sys_path = _sys_path("devices/system/cpu")
    info = []

    path = os.path.join(sys_path, "cpu%s/cpufreq/scaling_driver" % cpu)
    scaling_driver = _first_line(path, default='')
    if scaling_driver:
        info.append('driver:%s' % scaling_driver)

    if scaling_driver == 'intel_pstate':
        path = os.path.join(sys_path, "intel_pstate/no_turbo")
        no_turbo = _first_line(path, default='')
        if no_turbo == '1':
            info.append('intel_pstate:no turbo')
        elif no_turbo == '0':
            info.append('intel_pstate:turbo')
    else:
        boost = _get_cpu_boost(cpu)
        if boost is not None:
            if boost:
                info.append('boost:supported')
            else:
                info.append('boost:not suppported')

    path = os.path.join(sys_path, "cpu%s/cpufreq/scaling_governor" % cpu)
    scaling_governor = _first_line(path, default='')
    if scaling_governor:
        info.append('governor:%s' % scaling_governor)

    if not info:
        return None

    return ', '.join(info)


def _collect_cpu_config(metadata, cpus):
    configs = {}
    for cpu in cpus:
        config = _get_cpu_config(cpu)
        if config:
            configs[cpu] = config
    if not configs:
        return
    metadata['cpu_config'] = _format_cpu_infos(configs, cpus)


def _get_cpu_temperature(path, cpu_temp):
    hwmon_name = _first_line(os.path.join(path, 'name'), default='')
    if not hwmon_name.startswith('coretemp'):
        return

    index = 1
    while True:
        template = os.path.join(path, "temp%s_%%s" % index)

        try:
            temp_label = _first_line(template % 'label')
        except IOError:
            break

        temp_input = _first_line(template % 'input')
        temp_input = float(temp_input) / 1000
        # FIXME: On Python 2, u"%.0f\xb0C" introduces unicode errors if the
        # locale encoding is ASCII
        temp_input = "%.0f C" % temp_input

        item = '%s:%s=%s' % (hwmon_name, temp_label, temp_input)
        cpu_temp.append(item)

        index += 1


def _get_cpu_temperatures(metadata):
    path = _sys_path("class/hwmon")
    try:
        names = os.listdir(path)
    except OSError:
        return None

    cpu_temp = []
    for name in names:
        hwmon = os.path.join(path, name)
        _get_cpu_temperature(hwmon, cpu_temp)
    if not cpu_temp:
        return None

    metadata['cpu_temp'] = ', '.join(cpu_temp)


def _collect_cpu_affinity(metadata, cpu_affinity, cpu_count):
    if not cpu_affinity:
        return
    if not cpu_count:
        return

    # CPU affinity
    cpus = cpu_affinity
    if set(cpu_affinity) == set(range(cpu_count)):
        return

    isolated = perf._get_isolated_cpus()
    text = perf._format_cpu_list(cpu_affinity)
    if isolated and set(cpu_affinity) <= set(isolated):
        text = '%s (isolated)' % text
    metadata['cpu_affinity'] = text


def _collect_cpu_metadata(metadata):
    # CPU count
    cpu_count = _get_logical_cpu_count()
    if cpu_count:
        metadata['cpu_count'] = cpu_count

    cpu_affinity = _get_cpu_affinity()
    _collect_cpu_affinity(metadata, cpu_affinity, cpu_count)

    all_cpus = cpu_affinity
    if not all_cpus:
        all_cpus = _get_logical_cpu_count()

    if all_cpus:
        _collect_cpu_freq(metadata, all_cpus)
        _collect_cpu_config(metadata, all_cpus)

    _get_cpu_temperatures(metadata)


def _collect_metadata(metadata):
    metadata['perf_version'] = perf.__version__

    date = datetime.datetime.now().isoformat()
    # FIXME: Move date to a regular run attribute with type datetime.datetime?
    metadata['date'] = date.split('.', 1)[0]

    _collect_python_metadata(metadata)
    _collect_system_metadata(metadata)
    _collect_memory_metadata(metadata)
    _collect_cpu_metadata(metadata)
