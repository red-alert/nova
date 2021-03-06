#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os

from gabbi import fixture
from oslo_config import cfg
from oslo_config import fixture as config_fixture
from oslo_middleware import cors
from oslo_utils import uuidutils

from nova.api.openstack.placement import context
from nova.api.openstack.placement import deploy
from nova.api.openstack.placement import exception
from nova.api.openstack.placement.objects import consumer as consumer_obj
from nova.api.openstack.placement.objects import project as project_obj
from nova.api.openstack.placement.objects import resource_provider as rp_obj
from nova.api.openstack.placement.objects import user as user_obj
from nova.api.openstack.placement import policies
from nova import rc_fields as fields
from nova.tests import fixtures
from nova.tests.functional.api.openstack.placement.db import test_base as tb
from nova.tests.unit import policy_fixture
from nova.tests import uuidsentinel as uuids


CONF = cfg.CONF


def setup_app():
    return deploy.loadapp(CONF)


class APIFixture(fixture.GabbiFixture):
    """Setup the required backend fixtures for a basic placement service."""

    def start_fixture(self):
        # Set up stderr and stdout captures by directly driving the
        # existing nova fixtures that do that. This captures the
        # output that happens outside individual tests (for
        # example database migrations).
        self.standard_logging_fixture = fixtures.StandardLogging()
        self.standard_logging_fixture.setUp()
        self.output_stream_fixture = fixtures.OutputStreamCapture()
        self.output_stream_fixture.setUp()
        # Filter ignorable warnings during test runs.
        self.warnings_fixture = fixtures.WarningsFixture()
        self.warnings_fixture.setUp()

        self.conf_fixture = config_fixture.Config(CONF)
        self.conf_fixture.setUp()
        # The Database fixture will get confused if only one of the databases
        # is configured.
        for group in ('placement_database', 'api_database', 'database'):
            self.conf_fixture.config(
                group=group,
                connection='sqlite://',
                sqlite_synchronous=False)
        self.conf_fixture.config(
            group='api', auth_strategy='noauth2')

        self.context = context.RequestContext()

        # Register CORS opts, but do not set config. This has the
        # effect of exercising the "don't use cors" path in
        # deploy.py. Without setting some config the group will not
        # be present.
        CONF.register_opts(cors.CORS_OPTS, 'cors')

        # Make sure default_config_files is an empty list, not None.
        # If None /etc/nova/nova.conf is read and confuses results.
        CONF([], default_config_files=[])

        self._reset_db_flags()
        self.placement_db_fixture = fixtures.Database('placement')
        self.placement_db_fixture.setUp()
        # Do this now instead of waiting for the WSGI app to start so that
        # fixtures can have traits.
        deploy.update_database()

        os.environ['RP_UUID'] = uuidutils.generate_uuid()
        os.environ['RP_NAME'] = uuidutils.generate_uuid()
        os.environ['CUSTOM_RES_CLASS'] = 'CUSTOM_IRON_NFV'
        os.environ['PROJECT_ID'] = uuidutils.generate_uuid()
        os.environ['USER_ID'] = uuidutils.generate_uuid()
        os.environ['PROJECT_ID_ALT'] = uuidutils.generate_uuid()
        os.environ['USER_ID_ALT'] = uuidutils.generate_uuid()
        os.environ['INSTANCE_UUID'] = uuidutils.generate_uuid()
        os.environ['MIGRATION_UUID'] = uuidutils.generate_uuid()
        os.environ['CONSUMER_UUID'] = uuidutils.generate_uuid()
        os.environ['PARENT_PROVIDER_UUID'] = uuidutils.generate_uuid()
        os.environ['ALT_PARENT_PROVIDER_UUID'] = uuidutils.generate_uuid()

    def stop_fixture(self):
        self.placement_db_fixture.cleanUp()

        # Since we clean up the DB, we need to reset the traits sync
        # flag to make sure the next run will recreate the traits and
        # reset the _RC_CACHE so that any cached resource classes
        # are flushed.
        self._reset_db_flags()

        self.warnings_fixture.cleanUp()
        self.output_stream_fixture.cleanUp()
        self.standard_logging_fixture.cleanUp()
        self.conf_fixture.cleanUp()

    @staticmethod
    def _reset_db_flags():
        rp_obj._TRAITS_SYNCED = False
        rp_obj._RC_CACHE = None


class AllocationFixture(APIFixture):
    """An APIFixture that has some pre-made Allocations."""

    # TODO(jaypipes): Simplify and restructure this fixture
    def start_fixture(self):
        super(AllocationFixture, self).start_fixture()

        # For use creating and querying allocations/usages
        os.environ['ALT_USER_ID'] = uuidutils.generate_uuid()
        project_id = os.environ['PROJECT_ID']
        user_id = os.environ['USER_ID']
        alt_user_id = os.environ['ALT_USER_ID']

        user = user_obj.User(self.context, external_id=user_id)
        user.create()
        alt_user = user_obj.User(self.context, external_id=alt_user_id)
        alt_user.create()
        project = project_obj.Project(self.context, external_id=project_id)
        project.create()

        # Stealing from the super
        rp_name = os.environ['RP_NAME']
        rp_uuid = os.environ['RP_UUID']
        rp = rp_obj.ResourceProvider(
            self.context, name=rp_name, uuid=rp_uuid)
        rp.create()

        # Create a first consumer for the DISK_GB
        consumer_id = uuidutils.generate_uuid()
        consumer = consumer_obj.Consumer(
            self.context, uuid=consumer_id, user=user, project=project)
        consumer.create()

        # Create some DISK_GB inventory and allocations.
        inventory = rp_obj.Inventory(
            self.context, resource_provider=rp,
            resource_class='DISK_GB', total=2048,
            step_size=10, min_unit=10, max_unit=600)
        inventory.obj_set_defaults()
        rp.add_inventory(inventory)
        alloc1 = rp_obj.Allocation(
            self.context, resource_provider=rp,
            resource_class='DISK_GB',
            consumer=consumer,
            used=500)
        alloc2 = rp_obj.Allocation(
            self.context, resource_provider=rp,
            resource_class='DISK_GB',
            consumer=consumer,
            used=500)
        alloc_list = rp_obj.AllocationList(
            self.context,
            objects=[alloc1, alloc2]
        )
        alloc_list.replace_all()

        # Create a second consumer for the VCPU
        consumer_id = uuidutils.generate_uuid()
        consumer = consumer_obj.Consumer(
            self.context, uuid=consumer_id, user=user, project=project)
        consumer.create()
        # This consumer is referenced from the gabbits
        os.environ['CONSUMER_ID'] = consumer_id

        # Create some VCPU inventory and allocations.
        inventory = rp_obj.Inventory(
            self.context, resource_provider=rp,
            resource_class='VCPU', total=10,
            max_unit=4)
        inventory.obj_set_defaults()
        rp.add_inventory(inventory)
        alloc1 = rp_obj.Allocation(
            self.context, resource_provider=rp,
            resource_class='VCPU',
            consumer=consumer,
            used=2)
        alloc2 = rp_obj.Allocation(
            self.context, resource_provider=rp,
            resource_class='VCPU',
            consumer=consumer,
            used=4)
        alloc_list = rp_obj.AllocationList(
                self.context,
                objects=[alloc1, alloc2])
        alloc_list.replace_all()

        # Create a consumer object for a different user
        alt_consumer_id = uuidutils.generate_uuid()
        alt_consumer = consumer_obj.Consumer(
            self.context, uuid=alt_consumer_id, user=alt_user,
            project=project)
        alt_consumer.create()
        os.environ['ALT_CONSUMER_ID'] = alt_consumer_id

        # Create a couple of allocations for a different user.
        alloc1 = rp_obj.Allocation(
            self.context, resource_provider=rp,
            resource_class='DISK_GB',
            consumer=alt_consumer,
            used=20)
        alloc2 = rp_obj.Allocation(
            self.context, resource_provider=rp,
            resource_class='VCPU',
            consumer=alt_consumer,
            used=1)
        alloc_list = rp_obj.AllocationList(
                self.context,
                objects=[alloc1, alloc2])
        alloc_list.replace_all()

        # The ALT_RP_XXX variables are for a resource provider that has
        # not been created in the Allocation fixture
        os.environ['ALT_RP_UUID'] = uuidutils.generate_uuid()
        os.environ['ALT_RP_NAME'] = uuidutils.generate_uuid()


class SharedStorageFixture(APIFixture):
    """An APIFixture that has some two compute nodes without local storage
    associated by aggregate to a provider of shared storage. Both compute
    nodes have respectively two numa node resource providers, each of
    which has a pf resource provider.

                     +-------------------------------------+
                     |  sharing storage (ss)               |
                     |   DISK_GB:2000                      |
                     |   traits: MISC_SHARES_VIA_AGGREGATE |
                     +-----------------+-------------------+
                                       | aggregate
        +--------------------------+   |   +------------------------+
        | compute node (cn1)       |---+---| compute node (cn2)     |
        |  CPU: 24                 |       |  CPU: 24               |
        |  MEMORY_MB: 128*1024     |       |  MEMORY_MB: 128*1024   |
        |  traits: HW_CPU_X86_SSE, |       |                        |
        |          HW_CPU_X86_SSE2 |       |                        |
        +--------------------------+       +------------------------+
             |               |                 |                |
        +---------+      +---------+      +---------+      +---------+
        | numa1_1 |      | numa1_2 |      | numa2_1 |      | numa2_2 |
        +---------+      +---------+      +---------+      +---------+
             |                |                |                |
     +---------------++---------------++---------------++----------------+
     | pf1_1         || pf1_2         || pf2_1         || pf2_2          |
     | SRIOV_NET_VF:8|| SRIOV_NET_VF:8|| SRIOV_NET_VF:8|| SRIOV_NET_VF:8 |
     +---------------++---------------++---------------++----------------+
    """

    def start_fixture(self):
        super(SharedStorageFixture, self).start_fixture()

        agg_uuid = uuidutils.generate_uuid()

        cn1 = tb.create_provider(self.context, 'cn1', agg_uuid)
        cn2 = tb.create_provider(self.context, 'cn2', agg_uuid)
        ss = tb.create_provider(self.context, 'ss', agg_uuid)

        numa1_1 = tb.create_provider(self.context, 'numa1_1', parent=cn1.uuid)
        numa1_2 = tb.create_provider(self.context, 'numa1_2', parent=cn1.uuid)
        numa2_1 = tb.create_provider(self.context, 'numa2_1', parent=cn2.uuid)
        numa2_2 = tb.create_provider(self.context, 'numa2_2', parent=cn2.uuid)

        pf1_1 = tb.create_provider(self.context, 'pf1_1', parent=numa1_1.uuid)
        pf1_2 = tb.create_provider(self.context, 'pf1_2', parent=numa1_2.uuid)
        pf2_1 = tb.create_provider(self.context, 'pf2_1', parent=numa2_1.uuid)
        pf2_2 = tb.create_provider(self.context, 'pf2_2', parent=numa2_2.uuid)

        os.environ['AGG_UUID'] = agg_uuid

        os.environ['CN1_UUID'] = cn1.uuid
        os.environ['CN2_UUID'] = cn2.uuid
        os.environ['SS_UUID'] = ss.uuid

        os.environ['NUMA1_1_UUID'] = numa1_1.uuid
        os.environ['NUMA1_2_UUID'] = numa1_2.uuid
        os.environ['NUMA2_1_UUID'] = numa2_1.uuid
        os.environ['NUMA2_2_UUID'] = numa2_2.uuid

        os.environ['PF1_1_UUID'] = pf1_1.uuid
        os.environ['PF1_2_UUID'] = pf1_2.uuid
        os.environ['PF2_1_UUID'] = pf2_1.uuid
        os.environ['PF2_2_UUID'] = pf2_2.uuid

        # Populate compute node inventory for VCPU and RAM
        for cn in (cn1, cn2):
            tb.add_inventory(cn, fields.ResourceClass.VCPU, 24,
                             allocation_ratio=16.0)
            tb.add_inventory(cn, fields.ResourceClass.MEMORY_MB, 128 * 1024,
                             allocation_ratio=1.5)
        tb.set_traits(cn1, 'HW_CPU_X86_SSE', 'HW_CPU_X86_SSE2')

        # Populate shared storage provider with DISK_GB inventory and
        # mark it shared among any provider associated via aggregate
        tb.add_inventory(ss, fields.ResourceClass.DISK_GB, 2000,
                         reserved=100, allocation_ratio=1.0)
        tb.set_traits(ss, 'MISC_SHARES_VIA_AGGREGATE')

        # Populate PF inventory for VF
        for pf in (pf1_1, pf1_2, pf2_1, pf2_2):
            tb.add_inventory(pf, fields.ResourceClass.SRIOV_NET_VF,
                             8, allocation_ratio=1.0)


class NonSharedStorageFixture(APIFixture):
    """An APIFixture that has two compute nodes with local storage that do not
    use shared storage.
    """
    def start_fixture(self):
        super(NonSharedStorageFixture, self).start_fixture()

        cn1_uuid = uuidutils.generate_uuid()
        cn2_uuid = uuidutils.generate_uuid()
        aggA_uuid = uuidutils.generate_uuid()
        aggB_uuid = uuidutils.generate_uuid()
        aggC_uuid = uuidutils.generate_uuid()
        os.environ['CN1_UUID'] = cn1_uuid
        os.environ['CN2_UUID'] = cn2_uuid
        os.environ['AGGA_UUID'] = aggA_uuid
        os.environ['AGGB_UUID'] = aggB_uuid
        os.environ['AGGC_UUID'] = aggC_uuid

        cn1 = rp_obj.ResourceProvider(
            self.context,
            name='cn1',
            uuid=cn1_uuid)
        cn1.create()

        cn2 = rp_obj.ResourceProvider(
            self.context,
            name='cn2',
            uuid=cn2_uuid)
        cn2.create()

        # Populate compute node inventory for VCPU and RAM
        for cn in (cn1, cn2):
            vcpu_inv = rp_obj.Inventory(
                self.context,
                resource_provider=cn,
                resource_class='VCPU',
                total=24,
                reserved=0,
                max_unit=24,
                min_unit=1,
                step_size=1,
                allocation_ratio=16.0)
            vcpu_inv.obj_set_defaults()
            ram_inv = rp_obj.Inventory(
                self.context,
                resource_provider=cn,
                resource_class='MEMORY_MB',
                total=128 * 1024,
                reserved=0,
                max_unit=128 * 1024,
                min_unit=256,
                step_size=256,
                allocation_ratio=1.5)
            ram_inv.obj_set_defaults()
            disk_inv = rp_obj.Inventory(
                self.context,
                resource_provider=cn,
                resource_class='DISK_GB',
                total=2000,
                reserved=100,
                max_unit=2000,
                min_unit=10,
                step_size=10,
                allocation_ratio=1.0)
            disk_inv.obj_set_defaults()
            inv_list = rp_obj.InventoryList(objects=[vcpu_inv, ram_inv,
                    disk_inv])
            cn.set_inventory(inv_list)


class CORSFixture(APIFixture):
    """An APIFixture that turns on CORS."""

    def start_fixture(self):
        super(CORSFixture, self).start_fixture()
        # NOTE(cdent): If we remove this override, then the cors
        # group ends up not existing in the conf, so when deploy.py
        # wants to load the CORS middleware, it will not.
        self.conf_fixture.config(
            group='cors',
            allowed_origin='http://valid.example.com')


# TODO(efried): Common with test_allocation_candidates
def _add_inventory(rp, rc, total, **kwargs):
    kwargs.setdefault('max_unit', total)
    inv = rp_obj.Inventory(rp._context, resource_provider=rp,
                           resource_class=rc, total=total, **kwargs)
    inv.obj_set_defaults()
    rp.add_inventory(inv)


# TODO(efried): Common with test_allocation_candidates
def _set_traits(rp, *traits):
    tlist = []
    for tname in traits:
        try:
            trait = rp_obj.Trait.get_by_name(rp._context, tname)
        except exception.TraitNotFound:
            trait = rp_obj.Trait(rp._context, name=tname)
            trait.create()
        tlist.append(trait)
    rp.set_traits(rp_obj.TraitList(objects=tlist))


class GranularFixture(APIFixture):
    """An APIFixture that sets up the following provider environment for
    testing granular resource requests.

+========================++========================++========================+
|cn_left                 ||cn_middle               ||cn_right                |
|VCPU: 8                 ||VCPU: 8                 ||VCPU: 8                 |
|MEMORY_MB: 4096         ||MEMORY_MB: 4096         ||MEMORY_MB: 4096         |
|DISK_GB: 500            ||SRIOV_NET_VF: 8         ||DISK_GB: 500            |
|VGPU: 8                 ||CUSTOM_NET_MBPS: 4000   ||VGPU: 8                 |
|SRIOV_NET_VF: 8         ||traits: HW_CPU_X86_AVX, ||  - max_unit: 2         |
|CUSTOM_NET_MBPS: 4000   ||        HW_CPU_X86_AVX2,||traits: HW_CPU_X86_MMX, |
|traits: HW_CPU_X86_AVX, ||        HW_CPU_X86_SSE, ||        HW_GPU_API_DXVA,|
|        HW_CPU_X86_AVX2,||        HW_NIC_ACCEL_TLS||        CUSTOM_DISK_SSD,|
|        HW_GPU_API_DXVA,|+=+=====+================++==+========+============+
|        HW_NIC_DCB_PFC, |  :     :                    :        : a
|        CUSTOM_FOO      +..+     +--------------------+        : g
+========================+  : a   :                             : g
                            : g   :                             : C
+========================+  : g   :             +===============+======+
|shr_disk_1              |  : A   :             |shr_net               |
|DISK_GB: 1000           +..+     :             |SRIOV_NET_VF: 16      |
|traits: CUSTOM_DISK_SSD,|  :     : a           |CUSTOM_NET_MBPS: 40000|
|  MISC_SHARES_VIA_AGG...|  :     : g           |traits: MISC_SHARES...|
+========================+  :     : g           +======================+
+=======================+   :     : B
|shr_disk_2             +...+     :
|DISK_GB: 1000          |         :
|traits: MISC_SHARES... +.........+
+=======================+
    """
    def _create_provider(self, name, *aggs, **kwargs):
        # TODO(efried): Common with test_allocation_candidates.ProviderDBBase
        parent = kwargs.get('parent')
        rp = rp_obj.ResourceProvider(self.context, name=name,
                                     uuid=getattr(uuids, name))
        if parent:
            rp.parent_provider_uuid = parent
        rp.create()
        if aggs:
            rp.set_aggregates(aggs)
        return rp

    def start_fixture(self):
        super(GranularFixture, self).start_fixture()

        rp_obj.ResourceClass(
            context=self.context, name='CUSTOM_NET_MBPS').create()

        os.environ['AGGA'] = uuids.aggA
        os.environ['AGGB'] = uuids.aggB
        os.environ['AGGC'] = uuids.aggC

        cn_left = self._create_provider('cn_left', uuids.aggA)
        os.environ['CN_LEFT'] = cn_left.uuid
        _add_inventory(cn_left, 'VCPU', 8)
        _add_inventory(cn_left, 'MEMORY_MB', 4096)
        _add_inventory(cn_left, 'DISK_GB', 500)
        _add_inventory(cn_left, 'VGPU', 8)
        _add_inventory(cn_left, 'SRIOV_NET_VF', 8)
        _add_inventory(cn_left, 'CUSTOM_NET_MBPS', 4000)
        _set_traits(cn_left, 'HW_CPU_X86_AVX', 'HW_CPU_X86_AVX2',
                    'HW_GPU_API_DXVA', 'HW_NIC_DCB_PFC', 'CUSTOM_FOO')

        cn_middle = self._create_provider('cn_middle', uuids.aggA, uuids.aggB)
        os.environ['CN_MIDDLE'] = cn_middle.uuid
        _add_inventory(cn_middle, 'VCPU', 8)
        _add_inventory(cn_middle, 'MEMORY_MB', 4096)
        _add_inventory(cn_middle, 'SRIOV_NET_VF', 8)
        _add_inventory(cn_middle, 'CUSTOM_NET_MBPS', 4000)
        _set_traits(cn_middle, 'HW_CPU_X86_AVX', 'HW_CPU_X86_AVX2',
                    'HW_CPU_X86_SSE', 'HW_NIC_ACCEL_TLS')

        cn_right = self._create_provider('cn_right', uuids.aggB, uuids.aggC)
        os.environ['CN_RIGHT'] = cn_right.uuid
        _add_inventory(cn_right, 'VCPU', 8)
        _add_inventory(cn_right, 'MEMORY_MB', 4096)
        _add_inventory(cn_right, 'DISK_GB', 500)
        _add_inventory(cn_right, 'VGPU', 8, max_unit=2)
        _set_traits(cn_right, 'HW_CPU_X86_MMX', 'HW_GPU_API_DXVA',
                    'CUSTOM_DISK_SSD')

        shr_disk_1 = self._create_provider('shr_disk_1', uuids.aggA)
        os.environ['SHR_DISK_1'] = shr_disk_1.uuid
        _add_inventory(shr_disk_1, 'DISK_GB', 1000)
        _set_traits(shr_disk_1, 'MISC_SHARES_VIA_AGGREGATE', 'CUSTOM_DISK_SSD')

        shr_disk_2 = self._create_provider(
            'shr_disk_2', uuids.aggA, uuids.aggB)
        os.environ['SHR_DISK_2'] = shr_disk_2.uuid
        _add_inventory(shr_disk_2, 'DISK_GB', 1000)
        _set_traits(shr_disk_2, 'MISC_SHARES_VIA_AGGREGATE')

        shr_net = self._create_provider('shr_net', uuids.aggC)
        os.environ['SHR_NET'] = shr_net.uuid
        _add_inventory(shr_net, 'SRIOV_NET_VF', 16)
        _add_inventory(shr_net, 'CUSTOM_NET_MBPS', 40000)
        _set_traits(shr_net, 'MISC_SHARES_VIA_AGGREGATE')


class OpenPolicyFixture(APIFixture):
    """An APIFixture that changes all policy rules to allow non-admins."""

    def start_fixture(self):
        super(OpenPolicyFixture, self).start_fixture()
        self.placement_policy_fixture = policy_fixture.PlacementPolicyFixture()
        self.placement_policy_fixture.setUp()
        # Get all of the registered rules and set them to '@' to allow any
        # user to have access. The nova policy "admin_or_owner" concept does
        # not really apply to most of placement resources since they do not
        # have a user_id/project_id attribute.
        rules = {}
        for rule in policies.list_rules():
            name = rule.name
            # Ignore "base" rules for role:admin.
            if name in ['placement', 'admin_api']:
                continue
            rules[name] = '@'
        self.placement_policy_fixture.set_rules(rules)

    def stop_fixture(self):
        super(OpenPolicyFixture, self).stop_fixture()
        self.placement_policy_fixture.cleanUp()
