import sys
import os
from functools import wraps
import time
import urlparse
from uuid import UUID
from xml.etree import ElementTree

import libvirt

from ovf.OvfFile import OvfFile

from opennode.cli.actions.vm import kvm, openvz
from opennode.cli.actions.utils import roll_data, execute
from opennode.cli import config

__all__ = ['autodetected_backends', 'list_vms', 'info_vm', 'start_vm', 'shutdown_vm',
           'destroy_vm', 'reboot_vm', 'suspend_vm', 'resume_vm', 'deploy_vm',
           'undeploy_vm', 'get_local_templates', 'metrics']


vm_types = {
    'openvz': openvz,
    'kvm': kvm,
    'qemu': kvm,  # synonym for kvm in our case
}

_connections = {}


def vm_method(fun):
    @wraps(fun)
    def wrapper(backend, *args, **kwargs):
        conn = _connection(backend)

        try:
            return fun(conn, *args, **kwargs)
        finally:
            if backend.startswith('test://') and backend != 'test:///default':
                _dump_state(conn, '/tmp/func_vm_test_state.xml')

    return wrapper


def backends():
    return config.c('general', 'backends').split(',')


def backend_hname(uri):
    """Return human-friendly name of the backend from the libvirt URI"""
    names = {'openvz:///system': 'openvz',
             'qemu:///system': 'kvm',
             'xen:///system': 'xen'}
    return names[uri]


def autodetected_backends():
    auto = []
    if os.path.exists('/dev/vzctl'):
        auto.append('openvz:///system')
    if os.path.exists('/dev/kvm'):
        auto.append('qemu:///system')
    config.cs('general', 'backends', ','.join(auto))
    return auto


def _connection(backend):
    bs = backends()
    if bs and (backend not in bs and not backend.startswith('test://')):
        raise Exception("unsupported backend %s" % backend)

    conn = libvirt.open(backend)

    # implement the 'status="inactive"' extension in the test:/// xml dump
    # so that we can test more complex scenarios.
    if backend.startswith('test://') and backend != "test:///default":
        path = urlparse.urlparse(backend).path
        conf = ElementTree.parse(path)

        for node in conf.findall('//domain'):
            if node.attrib.get('state', None) == 'inactive':
                dom = conn.lookupByName(node.findtext('.//name'))
                dom.shutdown()

    return conn


def _dump_state(conn, filename):
    with open(filename, 'w') as f:
        os.chmod(filename, 0666)
        print >>f, '<?xml version="1.0"?>\n<node>'
        vms = _list_vms(conn)
        for vm in vms:
            xml = conn.lookupByName(vm['name']).XMLDesc(0)
            node = ElementTree.fromstring(xml)
            node.attrib['state'] = vm['state']

            print >>f, ElementTree.tostring(node)
        print >>f, '</node>'


def dom_dom(conn, uuid):
    return ElementTree.fromstring(conn.lookupByUUIDString(uuid).XMLDesc(0))


def list_vm_ids(backend):
    conn = _connection(backend)
    return map(str, conn.listDefinedDomains() + conn.listDomainsID())


def _render_vm(conn, vm):
    def get_uuid(vm):
        return str(UUID(bytes=vm.UUID()))

    STATE_MAP = {
       0: "active",
       1: "active",
       2: "active",
       3: "suspended",
       4: "inactive",
       5: "inactive",
       6: "inactive"
    }

    RUN_STATE_MAP = {
       0: "no_state",
       1: "running",
       2: "blocked",
       3: "suspended",
       4: "shutting_down",
       5: "shutoff",
       6: "crashed"
    }

    info = vm.info()

    def vm_name(vm):
        if conn.getType() == 'OpenVZ':
            return openvz.get_hostname(vm.name())
        return vm.name()

    def vm_template_name(vm):
        if conn.getType() == 'OpenVZ':
            return openvz.get_template_name(vm.name())
        return None

    def vm_memory(vm):
        # libvirt doesn't work with openvz
        if conn.getType() == 'OpenVZ':
            return openvz.get_memory(vm.name())
        # XXX: todo use libvirt
        return 0

    def vm_uptime(vm, state):
        if state != 'active':
            return None

        # libvirt doesn't work with openvz
        if conn.getType() == 'OpenVZ':
            return openvz.get_uptime(vm.name())
        # XXX: todo use libvirt
        return 0

    def vm_diskspace(vm):
        if conn.getType() == 'OpenVZ':
            return {'/': openvz.get_diskspace(vm.name())}
        return {'/': 0.0}

    def vm_swap(vm):
        if conn.getType() == 'OpenVZ':
            return openvz.get_swap(vm.name())
        # XXX use libvirt
        return 0

    return {"uuid": get_uuid(vm), "name": vm_name(vm), "memory": vm_memory(vm),
            "uptime": vm_uptime(vm, STATE_MAP[info[0]]),
            "diskspace": vm_diskspace(vm),
            "template": vm_template_name(vm), "state": STATE_MAP[info[0]],
            "run_state": RUN_STATE_MAP[info[0]],
            "vm_uri": conn.getURI(),
            "vm_type": conn.getType().lower(),
            "swap": vm_swap(vm),
            "vcpu": vm.info()[3],
            'consoles': [i for i in [_vm_console_vnc(conn, get_uuid(vm)),
                                     _vm_console_pty(conn, get_uuid(vm))] if i],
            'interfaces': _vm_interfaces(conn, get_uuid(vm))}


def _list_vms(conn):
    online = []
    online += [_render_vm(conn, vm) for vm in (conn.lookupByID(i) for i \
                                                    in _get_running_vm_ids(conn))]
    offline = [_render_vm(conn, vm) for vm in (conn.lookupByName(i) for i \
                                               in conn.listDefinedDomains())]
    return online + offline


def free_mem():
    """Taken from func's Virt module,
    and adapted to handle multiple backends.

    The free memory is a concept which goes accross multiple virtualization
    backends, so it's the only method which doesn't require a specific backend
    parameter.

    """
    backends = backends()

    # Start with the physical memory and subtract
    memory = _connection(backends[0]).getInfo()[1]

    # Take 256M off which is reserved for Domain-0
    memory = memory - 256

    for conn in (_connection(b) for b in backends):
        for vm in (conn.lookupByID(i) for i in conn.listDomainsID()):
            # Exclude stopped vms and Domain-0 by using
            # ids greater than 0
            # NOTE: is this needed ? Seems that with kvm and lxc dom-0 is not
            # reported
            if vm.ID() > 0:
                # This node is active - remove its memory (in bytes)
                memory = memory - int(vm.info()[2]) / 1024

    return memory


@vm_method
def list_vms(conn):
    return _list_vms(conn)


@vm_method
def info_vm(conn, uuid):
    dom = conn.lookupByUUIDString(uuid)
    return _render_vm(dom)


@vm_method
def start_vm(conn, uuid):
    dom = conn.lookupByUUIDString(uuid)
    dom.create()


@vm_method
def shutdown_vm(conn, uuid):
    # XXX hack for OpenVZ because of a bad libvirt driver
    if conn.getType() == 'OpenVZ':
        openvz.shutdown_vm(uuid)
    else:
        dom = conn.lookupByUUIDString(uuid)
        dom.shutdown()


@vm_method
def destroy_vm(conn, uuid):
    dom = conn.lookupByUUIDString(uuid)
    dom.destroy()


@vm_method
def reboot_vm(conn, uuid):
    dom = conn.lookupByUUIDString(uuid)
    try:
        dom.reboot(0)
    except libvirt.libvirtError as e:
        if e.get_error_code() == libvirt.VIR_ERR_NO_SUPPORT:
            dom.shutdown()
            while True:
                try:
                    if dom.info()[0] == 5:
                        break
                    time.sleep(1)
                except libvirt.libvirtError as e:
                    # see opennode-management #34, or
                    # https://bugzilla.redhat.com/show_bug.cgi?id=519667
                    if e.get_error_domain() == libvirt.VIR_FROM_QEMU and \
                        e.get_error_code() == libvirt.VIR_ERR_OPERATION_FAILED:
                        continue
                    raise e
            dom.create()


@vm_method
def suspend_vm(conn, uuid):
    dom = conn.lookupByUUIDString(uuid)
    dom.suspend()


@vm_method
def resume_vm(conn, uuid):
    dom = conn.lookupByUUIDString(uuid)
    dom.resume()


@vm_method
def deploy_vm(conn, vm_parameters):
    # XXX Disabled logger for now. In it's current form it introduces dependency
    # on the func architecture, actions should have their own logging system,
    # which can be set to use func's logging
    try:
        _deploy_vm(vm_parameters)
    except Exception as e:
        raise e
    return "OK"


@vm_method
def undeploy_vm(conn, uuid):
    dom = conn.lookupByUUIDString(uuid)
    dom.undefine()


@vm_method
def get_local_templates(conn):
    vm_type = conn.getType().lower()
    tmpls = []
    from opennode.cli.actions.templates import get_template_info, \
                get_local_templates as local_templates
    for tmpl_name in local_templates(vm_type):
        tmpl_data = get_template_info(tmpl_name, vm_type)
        tmpl_data['template_name'] = tmpl_name
        tmpls.append(tmpl_data)
    return tmpls


def _vm_console_vnc(conn, uuid):
    # python 2.6 etree library doesn't support xpath with predicate
    element = ([i for i in dom_dom(conn, uuid).findall('.//graphics') if \
                    i.attrib.get('type', None) == 'vnc'] or [None])[0]
    # elementtree element without children is treated as false
    if element != None:
        port = element.attrib.get('port', None)
        if port and port != '-1':
            return dict(type='vnc', port=port)

vm_console_vnc = vm_method(_vm_console_vnc)


def _vm_console_pty(conn, uuid):
    # python 2.6 etree library doesn't support xpath with predicate
    element = ([i for i in dom_dom(conn, uuid).findall('.//console') if \
                    i.attrib.get('type', None) == 'pty'] or [None])[0]
    if element != None:
        pty = element.attrib.get('tty', None)
        if pty:
            return dict(type='pty', pty=pty)
    elif conn.getType() == 'OpenVZ':
        return dict(type='openvz', cid=conn.lookupByUUIDString(uuid).name())


vm_console_pty = vm_method(_vm_console_pty)


def _vm_interfaces(conn, uuid):
    elements = dom_dom(conn, uuid).findall('.//interface')

    def interface(idx, i):
        type = i.attrib.get('type')
        if type == 'network' and (i.find('forward') == None or \
                        i.find('forward').attrib.get('mode', None) == 'nat'):
            type = 'nat'

        mac = i.find('mac').attrib.get('address', None)

        alias = i.find('alias')
        if alias == None:
            alias = 'eth%s' % idx
        else:
            alias = alias.attrib.get('name', None)

        res = dict(mac=mac, name=alias, type=type)

        ip = i.find('ip')
        if ip != None:
            res['ipv4_address'] = ip.attrib.get('address', None)
            if not '/' in res['ipv4_address']:
                res['ipv4_address'] += '/24'

        return res

    return [interface(idx, i) for idx, i in enumerate(elements)]

vm_interfaces = vm_method(_vm_interfaces)


@vm_method
def metrics(conn):

    if conn.getType() != 'OpenVZ':
        return {}

    def get_uuid(vm):
        return str(UUID(bytes=vm.UUID()))

    def vm_metrics(vm):
        def cpu_usage():
            time_list_now = map(int, execute("vzctl exec %s \"head -n 1 /proc/stat\"" % vm.ID()).split(' ')[2:6])
            time_list_was = roll_data('/tmp/func-cpu-%s' % vm.ID(), time_list_now, [0] * 6)
            deltas = [yi - xi for yi, xi in zip(time_list_now, time_list_was)]
            try:
                cpu_pct = 1 - (float(deltas[-1]) / sum(deltas))
            except ZeroDivisionError:
                cpu_pct = 0
            return cpu_pct

        def load():
            return float(execute("vzctl exec %s \"cat /proc/loadavg | awk '{print \$1}'\"" % vm.ID()))

        def memory_usage():
            return float(execute("vzctl exec %s \"free | tail -n 2 | head -n 1 |awk '{print \$3 / 1024}'\"" % vm.ID()))

        def network_usage():
            def get_netstats():
                return [int(v) for v in execute("vzctl exec %s \"cat /proc/net/dev|grep venet0 | awk -F: '{print \$2}' | awk '{print \$1, \$9}'\""
                                                % vm.ID()).split(' ')]

            t2, (rx2, tx2) = time.time(), get_netstats()
            t1, rx1, tx1 = roll_data("/tmp/func-network-%s" % vm.ID(), (t2, rx2, tx2), (0, 0, 0))

            window = t2 - t1
            return ((rx2 - rx1) / window, (tx2 - tx1) / window)

        def diskspace_usage():
            return float(execute("vzctl exec %s \"df -P |grep ' /\$' | head -n 1 | awk '{print \$3/1024}'\"" % vm.ID()))

        return dict(cpu_usage=cpu_usage(),
                    load=load(),
                    memory_usage=memory_usage(),
                    network_usage=max(network_usage()),
                    diskspace_usage=diskspace_usage())
    try:
        return dict((get_uuid(vm), vm_metrics(vm)) for vm in (conn.lookupByID(i) for i in conn.listDomainsID()))
    except libvirt.libvirtError:
        return {}


def get_module(vm_type):
    try:
        return vm_types[vm_type]
    except KeyError:
        raise NotImplementedError("VM type '%s' is not (yet) supported" % vm_type)


def _deploy_vm(vm_parameters, logger=None):
    from opennode.cli import actions

    storage_pool = actions.storage.get_default_pool()
    if storage_pool is None:
        raise  Exception("Storage pool not defined")

    vm_type = vm_parameters['vm_type']

    template = vm_parameters['template_name']

    if not template:
        if logger:
            logger("Cannot deploy because template is '%s'" % (template))
        raise Exception("Cannot deploy because template is '%s'" % (template))
        return

    ovf_file = OvfFile(os.path.join(config.c("general", "storage-endpoint"),
                                    storage_pool, vm_type, "unpacked",
                                    template + ".ovf"))
    vm = actions.vm.get_module(vm_type)
    template_settings = vm.get_ovf_template_settings(ovf_file)

    template_settings.update(vm_parameters)

    errors = vm.adjust_setting_to_systems_resources(template_settings)
    if errors:
        if logger:
            logger("Got %s" % (errors,))
        raise  Exception("got errors %s" % (errors,))

    vm.deploy(template_settings, storage_pool)


def _get_running_vm_ids(conn):
    # XXX a workaround for libvirt's listDomainsID function throwing error _and_
    # screwing up snack screen if 0 openvz VMs available and no other backends present
    if conn.getType() == 'OpenVZ' and \
        'missing' == execute("vzlist -H > /dev/null 2>&1; if [  $? -eq 1 ]; then echo missing; fi"):
        return []
    else:
        return conn.listDomainsID()
