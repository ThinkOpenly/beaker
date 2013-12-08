import sys
import re
from turbogears.database import metadata, session
from turbogears.config import get
from turbogears import url
from copy import copy
from sqlalchemy import (Table, Column, Index, ForeignKey, UniqueConstraint,
                        String, Unicode, Integer, BigInteger, DateTime,
                        UnicodeText, Boolean, Float, VARCHAR, TEXT, Numeric,
                        or_, and_, not_, select, case, func, BigInteger)

from sqlalchemy.orm import relation, backref, dynamic_loader, \
        object_mapper, mapper, column_property, contains_eager, \
        relationship, class_mapper
from sqlalchemy.orm.interfaces import AttributeExtension
from sqlalchemy.orm.attributes import NEVER_SET
from sqlalchemy.orm.util import has_identity
from sqlalchemy.sql import exists, union, literal
from sqlalchemy.exc import InvalidRequestError, OperationalError
from sqlalchemy.orm.exc import NoResultFound
from bkr.server.installopts import InstallOptions, global_install_options
from sqlalchemy.orm.collections import attribute_mapped_collection
from sqlalchemy.ext.associationproxy import association_proxy
import time
from kid import Element, XML
from markdown import markdown
from bkr.server.bexceptions import BeakerException, BX, \
        VMCreationFailedException, StaleTaskStatusException, \
        InsufficientSystemPermissions, StaleSystemUserException, \
        StaleCommandStatusException, NoChangeException
from bkr.server.hybrid import hybrid_property, hybrid_method
from bkr.server.helpers import make_link, make_fake_link
from bkr.server.util import unicode_truncate, absolute_url, run_createrepo
from bkr.server import mail, metrics, identity
import os
import shutil
import urllib
import urlparse
import string
import lxml.etree
import uuid
import netaddr
import ovirtsdk.api
from collections import defaultdict
from datetime import timedelta, datetime
from hashlib import md5
import xml.dom.minidom

# These are only here for TaskLibrary. It would be nice to factor that out,
# but there's a circular dependency between Task and TaskLibrary
import subprocess
import rpm
from rhts import testinfo
from bkr.common.helpers import (AtomicFileReplacement, Flock,
                                makedirs_ignore, unlink_ignore)

from .base import DeclBase, MappedObject, SystemObject
from .types import (TaskStatus, CommandStatus, TaskResult, TaskPriority,
        SystemStatus, SystemType, ReleaseAction, ImageType, ResourceType,
        RecipeVirtStatus, SystemPermission, UUID, MACAddress,
        mac_unix_padded_dialect)
from .activity import Activity, ActivityMixin, activity_table
from .config import ConfigItem
from .identity import (User, Group, Permission, SSHPubKey, SystemGroup,
        UserGroup, UserActivity, GroupActivity, users_table)
from .lab import LabController, LabControllerActivity, lab_controller_table
from .distrolibrary import (Arch, KernelType, OSMajor, OSVersion,
        OSMajorInstallOptions, Distro, DistroTree, DistroTreeImage,
        DistroTreeRepo, DistroTag, DistroActivity, DistroTreeActivity,
        LabControllerDistroTree, kernel_type_table, arch_table,
        osmajor_table, distro_table, distro_tree_table,
        distro_tree_lab_controller_map)

import logging
log = logging.getLogger(__name__)

xmldoc = xml.dom.minidom.Document()

def node(element, value):
    node = xmldoc.createElement(element)
    node.appendChild(xmldoc.createTextNode(value))
    return node

hypervisor_table = Table('hypervisor', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('hypervisor', Unicode(100), nullable=False),
    mysql_engine='InnoDB',
)

system_table = Table('system', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('fqdn', Unicode(255), nullable=False),
    Column('serial', Unicode(1024)),
    Column('date_added', DateTime, 
           default=datetime.utcnow, nullable=False),
    Column('date_modified', DateTime),
    Column('date_lastcheckin', DateTime),
    Column('location', String(255)),
    Column('vendor', Unicode(255)),
    Column('model', Unicode(255)),
    Column('lender', Unicode(255)),
    Column('owner_id', Integer,
           ForeignKey('tg_user.user_id'), nullable=False),
    Column('user_id', Integer,
           ForeignKey('tg_user.user_id')),
    Column('type', SystemType.db_type(), nullable=False),
    Column('status', SystemStatus.db_type(), nullable=False),
    Column('status_reason',Unicode(255)),
    Column('private', Boolean, default=False),
    Column('deleted', Boolean, default=False),
    Column('memory', Integer),
    Column('checksum', String(32)),
    Column('lab_controller_id', Integer, ForeignKey('lab_controller.id')),
    Column('mac_address',String(18)),
    Column('loan_id', Integer,
           ForeignKey('tg_user.user_id')),
    Column('loan_comment', Unicode(1000),),
    Column('release_action', ReleaseAction.db_type()),
    Column('reprovision_distro_tree_id', Integer,
           ForeignKey('distro_tree.id')),
    Column('hypervisor_id', Integer,
           ForeignKey('hypervisor.id')),
    Column('kernel_type_id', Integer,
           ForeignKey('kernel_type.id'),
           default=select([kernel_type_table.c.id], limit=1).where(kernel_type_table.c.kernel_type==u'default').correlate(None),
           nullable=False),
    mysql_engine='InnoDB',
)

system_cc_table = Table('system_cc', metadata,
        Column('system_id', Integer, ForeignKey('system.id', ondelete='CASCADE',
            onupdate='CASCADE'), primary_key=True),
        Column('email_address', Unicode(255), primary_key=True, index=True),
        mysql_engine='InnoDB',
)

system_device_map = Table('system_device_map', metadata,
    Column('system_id', Integer,
           ForeignKey('system.id', onupdate='CASCADE', ondelete='CASCADE'),
           primary_key=True),
    Column('device_id', Integer,
           ForeignKey('device.id'),
           primary_key=True),
    mysql_engine='InnoDB',
)

system_arch_map = Table('system_arch_map', metadata,
    Column('system_id', Integer,
           ForeignKey('system.id', onupdate='CASCADE', ondelete='CASCADE'),
           primary_key=True),
    Column('arch_id', Integer,
           ForeignKey('arch.id'),
           primary_key=True),
    mysql_engine='InnoDB',
)

provision_table = Table('provision', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('system_id', Integer, ForeignKey('system.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False),
    Column('ks_meta', String(1024)),
    Column('kernel_options', String(1024)),
    Column('kernel_options_post', String(1024)),
    Column('arch_id', Integer, ForeignKey('arch.id'), nullable=False),
    mysql_engine='InnoDB',
)

provision_family_table = Table('provision_family', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('provision_id', Integer, ForeignKey('provision.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False),
    Column('osmajor_id', Integer, ForeignKey('osmajor.id'), nullable=False),
    Column('ks_meta', String(1024)),
    Column('kernel_options', String(1024)),
    Column('kernel_options_post', String(1024)),
    mysql_engine='InnoDB',
)

provision_family_update_table = Table('provision_update_family', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('provision_family_id', Integer, ForeignKey('provision_family.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False),
    Column('osversion_id', Integer, ForeignKey('osversion.id'), nullable=False),
    Column('ks_meta', String(1024)),
    Column('kernel_options', String(1024)),
    Column('kernel_options_post', String(1024)),
    mysql_engine='InnoDB',
)

exclude_osmajor_table = Table('exclude_osmajor', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('system_id', Integer, ForeignKey('system.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False),
    Column('arch_id', Integer, ForeignKey('arch.id'), nullable=False),
    Column('osmajor_id', Integer, ForeignKey('osmajor.id'), nullable=False),
    mysql_engine='InnoDB',
)

exclude_osversion_table = Table('exclude_osversion', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('system_id', Integer, ForeignKey('system.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False),
    Column('arch_id', Integer, ForeignKey('arch.id'), nullable=False),
    Column('osversion_id', Integer, ForeignKey('osversion.id'), nullable=False),
    mysql_engine='InnoDB',
)

task_exclude_arch_table = Table('task_exclude_arch', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('task_id', Integer, ForeignKey('task.id')),
    Column('arch_id', Integer, ForeignKey('arch.id')),
    mysql_engine='InnoDB',
)

task_exclude_osmajor_table = Table('task_exclude_osmajor', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('task_id', Integer, ForeignKey('task.id')),
    Column('osmajor_id', Integer, ForeignKey('osmajor.id')),
    mysql_engine='InnoDB',
)

labinfo_table = Table('labinfo', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('system_id', Integer, ForeignKey('system.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False),
    Column('orig_cost', Numeric(precision=16, scale=2, asdecimal=True)),
    Column('curr_cost', Numeric(precision=16, scale=2, asdecimal=True)),
    Column('dimensions', String(255)),
    Column('weight', Numeric(asdecimal=False)),
    Column('wattage', Numeric(asdecimal=False)),
    Column('cooling', Numeric(asdecimal=False)),
    mysql_engine='InnoDB',
)

watchdog_table = Table('watchdog', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('recipe_id', Integer, ForeignKey('recipe.id'), nullable=False),
    Column('recipetask_id', Integer, ForeignKey('recipe_task.id')),
    Column('subtask', Unicode(255)),
    Column('kill_time', DateTime),
    mysql_engine='InnoDB',
)

cpu_table = Table('cpu', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('system_id', Integer, ForeignKey('system.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False),
    Column('vendor',String(255)),
    Column('model',Integer),
    Column('model_name',String(255)),
    Column('family',Integer),
    Column('stepping',Integer),
    Column('speed',Float),
    Column('processors',Integer),
    Column('cores',Integer),
    Column('sockets',Integer),
    Column('hyper',Boolean),
    mysql_engine='InnoDB',
)

cpu_flag_table = Table('cpu_flag', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('cpu_id', Integer, ForeignKey('cpu.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False),
    Column('flag', String(255)),
    mysql_engine='InnoDB',
)

numa_table = Table('numa', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('system_id', Integer, ForeignKey('system.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False),
    Column('nodes',Integer),
    mysql_engine='InnoDB',
)

device_class_table = Table('device_class', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column("device_class", VARCHAR(24), nullable=False, unique=True),
    Column("description", TEXT),
    mysql_engine='InnoDB',
)

device_table = Table('device', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('vendor_id',String(4)),
    Column('device_id',String(4)),
    Column('subsys_device_id',String(4)),
    Column('subsys_vendor_id',String(4)),
    Column('bus',String(255)),
    Column('driver', String(255), index=True),
    Column('description',String(255)),
    Column('device_class_id', Integer,
           ForeignKey('device_class.id'), nullable=False),
    Column('date_added', DateTime, 
           default=datetime.utcnow, nullable=False),
    UniqueConstraint('vendor_id', 'device_id', 'subsys_device_id',
           'subsys_vendor_id', 'bus', 'driver', 'description', name='device_uix_1'),
    mysql_engine='InnoDB',
)
Index('ix_device_pciid', device_table.c.vendor_id, device_table.c.device_id)

disk_table = Table('disk', metadata,
    Column('id', Integer, autoincrement=True,
        nullable=False, primary_key=True),
    Column('system_id', Integer, ForeignKey('system.id'), nullable=False),
    Column('model', String(255)),
    # sizes are in bytes
    Column('size', BigInteger),
    Column('sector_size', Integer),
    Column('phys_sector_size', Integer),
    mysql_engine='InnoDB',
)

power_type_table = Table('power_type', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('name', String(255), nullable=False),
    mysql_engine='InnoDB',
)

power_table = Table('power', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('power_type_id', Integer, ForeignKey('power_type.id'),
           nullable=False),
    Column('system_id', Integer, ForeignKey('system.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False),
    Column('power_address', String(255), nullable=False),
    Column('power_user', String(255)),
    Column('power_passwd', String(255)),
    Column('power_id', String(255)),
    mysql_engine='InnoDB',
)

recipe_set_nacked_table = Table('recipe_set_nacked', metadata,
    Column('recipe_set_id', Integer, ForeignKey('recipe_set.id',
        onupdate='CASCADE', ondelete='CASCADE'), primary_key=True,nullable=False), 
    Column('response_id', Integer, ForeignKey('response.id', 
        onupdate='CASCADE', ondelete='CASCADE'), nullable=False),
    Column('comment', Unicode(255),nullable=True),
    Column('created',DateTime,nullable=False,default=datetime.utcnow),
    mysql_engine='InnoDB',
)

beaker_tag_table = Table('beaker_tag', metadata,
    Column('id', Integer, primary_key=True, nullable = False),
    Column('tag', Unicode(20), nullable=False),
    Column('type', Unicode(40), nullable=False),
    UniqueConstraint('tag', 'type'),
    mysql_engine='InnoDB',
)

retention_tag_table = Table('retention_tag', metadata,
    Column('id', Integer, ForeignKey('beaker_tag.id', onupdate='CASCADE', ondelete='CASCADE'),nullable=False, primary_key=True),
    Column('default_', Boolean),
    Column('expire_in_days', Integer, default=0),
    Column('needs_product', Boolean),
    mysql_engine='InnoDB',
)

product_table = Table('product', metadata,
    Column('id', Integer, autoincrement=True, nullable=False,
        primary_key=True),
    Column('name', Unicode(100),unique=True, index=True, nullable=False),
    Column('created', DateTime, nullable=False, default=datetime.utcnow),
    mysql_engine='InnoDB',
)

response_table = Table('response', metadata,
    Column('id', Integer, autoincrement=True, primary_key=True, nullable=False),
    Column('response',Unicode(50), nullable=False),
    mysql_engine='InnoDB',
)

system_activity_table = Table('system_activity', metadata,
    Column('id', Integer, ForeignKey('activity.id'), primary_key=True),
    Column('system_id', Integer, ForeignKey('system.id'), nullable=True),
    mysql_engine='InnoDB',
)

recipeset_activity_table = Table('recipeset_activity', metadata,
    Column('id', Integer,ForeignKey('activity.id'), primary_key=True),
    Column('recipeset_id', Integer, ForeignKey('recipe_set.id')),
    mysql_engine='InnoDB',
)

command_queue_table = Table('command_queue', metadata,
    Column('id', Integer, ForeignKey('activity.id'), primary_key=True),
    Column('system_id', Integer, ForeignKey('system.id',
           onupdate='CASCADE', ondelete='CASCADE'), nullable=False),
    Column('status', CommandStatus.db_type(), nullable=False),
    Column('task_id', String(255)),
    Column('delay_until', DateTime, default=None),
    Column('updated', DateTime, default=datetime.utcnow),
    Column('callback', String(255)),
    Column('distro_tree_id', Integer, ForeignKey('distro_tree.id')),
    Column('kernel_options', UnicodeText),
    mysql_engine='InnoDB',
)

# note schema
note_table = Table('note', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('system_id', Integer, ForeignKey('system.id', onupdate='CASCADE',
           ondelete='CASCADE'), nullable=False, index=True),
    Column('user_id', Integer, ForeignKey('tg_user.user_id'), index=True),
    Column('created', DateTime, nullable=False, default=datetime.utcnow),
    Column('text',TEXT, nullable=False),
    Column('deleted', DateTime, nullable=True, default=None),
    mysql_engine='InnoDB',
)

key_table = Table('key_', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('key_name', String(50), nullable=False, unique=True),
    Column('numeric', Boolean, default=False),
    mysql_engine='InnoDB',
)

key_value_string_table = Table('key_value_string', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('system_id', Integer, ForeignKey('system.id',
            onupdate='CASCADE', ondelete='CASCADE'), nullable=False, index=True),
    Column('key_id', Integer, ForeignKey('key_.id',
            onupdate='CASCADE', ondelete='CASCADE'), nullable=False, index=True),
    Column('key_value',TEXT, nullable=False),
    mysql_engine='InnoDB',
)

key_value_int_table = Table('key_value_int', metadata,
    Column('id', Integer, autoincrement=True,
           nullable=False, primary_key=True),
    Column('system_id', Integer, ForeignKey('system.id',
            onupdate='CASCADE', ondelete='CASCADE'), nullable=False, index=True),
    Column('key_id', Integer, ForeignKey('key_.id',
            onupdate='CASCADE', ondelete='CASCADE'), nullable=False, index=True),
    Column('key_value',Integer, nullable=False),
    mysql_engine='InnoDB',
)

job_table = Table('job',metadata,
        Column('id', Integer, primary_key=True),
        Column('dirty_version', UUID, nullable=False),
        Column('clean_version', UUID, nullable=False),
        Column('owner_id', Integer,
                ForeignKey('tg_user.user_id'), index=True),
        Column('submitter_id', Integer,
                ForeignKey('tg_user.user_id', name='job_submitter_id_fk')),
        Column('group_id', Integer, ForeignKey('tg_group.group_id', \
            name='job_group_id_fk'), default=None),
        Column('whiteboard',Unicode(2000)),
        Column('retention_tag_id', Integer, ForeignKey('retention_tag.id'), nullable=False),
        Column('product_id', Integer, ForeignKey('product.id'),nullable=True),
        Column('result', TaskResult.db_type(), nullable=False,
                default=TaskResult.new, index=True),
        Column('status', TaskStatus.db_type(), nullable=False,
                default=TaskStatus.new, index=True),
        Column('deleted', DateTime, default=None, index=True),
        Column('to_delete', DateTime, default=None, index=True),
        # Total tasks
	Column('ttasks', Integer, default=0),
        # Total Passing tasks
        Column('ptasks', Integer, default=0),
        # Total Warning tasks
        Column('wtasks', Integer, default=0),
        # Total Failing tasks
        Column('ftasks', Integer, default=0),
        # Total Panic tasks
        Column('ktasks', Integer, default=0),
        mysql_engine='InnoDB',
)
# for fast dirty_version != clean_version comparisons:
Index('ix_job_dirty_clean_version', job_table.c.dirty_version, job_table.c.clean_version)

job_cc_table = Table('job_cc', metadata,
        Column('job_id', Integer, ForeignKey('job.id', ondelete='CASCADE',
            onupdate='CASCADE'), primary_key=True),
        Column('email_address', Unicode(255), primary_key=True, index=True),
        mysql_engine='InnoDB',
)

recipe_set_table = Table('recipe_set',metadata,
        Column('id', Integer, primary_key=True),
        Column('job_id', Integer,
                ForeignKey('job.id'), nullable=False),
        Column('priority', TaskPriority.db_type(), nullable=False,
                default=TaskPriority.default_priority(), index=True),
        Column('queue_time',DateTime, nullable=False, default=datetime.utcnow),
        Column('result', TaskResult.db_type(), nullable=False,
                default=TaskResult.new, index=True),
        Column('status', TaskStatus.db_type(), nullable=False,
                default=TaskStatus.new, index=True),
        Column('lab_controller_id', Integer,
                ForeignKey('lab_controller.id')),
        # Total tasks
	Column('ttasks', Integer, default=0),
        # Total Passing tasks
        Column('ptasks', Integer, default=0),
        # Total Warning tasks
        Column('wtasks', Integer, default=0),
        # Total Failing tasks
        Column('ftasks', Integer, default=0),
        # Total Panic tasks
        Column('ktasks', Integer, default=0),
        mysql_engine='InnoDB',
)

# Log tables all have the following fields:
#   path
#       Subdirectory of this log, relative to the root of the recipe/RT/RTR. 
#       Probably won't have an initial or trailing slash, but I wouldn't bet on 
#       it. ;-) Notably, the value '/' is used (rather than the empty string) 
#       to represent no subdirectory.
#   filename
#       Filename of this log.
#   server
#       Absolute URL to the directory where the log is stored. Path and 
#       filename are relative to this.
#       Always NULL if log transferring is not enabled (CACHE=False).
#   basepath
#       Absolute filesystem path to the directory where the log is stored on 
#       the remote system. XXX we shouldn't need to store this!
#       Always NULL if log transferring is not enabled (CACHE=False).

log_recipe_table = Table('log_recipe', metadata,
        Column('id', Integer, primary_key=True),
        Column('recipe_id', Integer, ForeignKey('recipe.id'),
            nullable=False),
        Column('path', UnicodeText()),
        Column('filename', UnicodeText(), nullable=False),
        Column('start_time',DateTime, default=datetime.utcnow),
	Column('server', UnicodeText()),
	Column('basepath', UnicodeText()),
        mysql_engine='InnoDB',
)

log_recipe_task_table = Table('log_recipe_task', metadata,
        Column('id', Integer, primary_key=True),
        Column('recipe_task_id', Integer, ForeignKey('recipe_task.id'),
            nullable=False),
        Column('path', UnicodeText()),
        Column('filename', UnicodeText(), nullable=False),
        Column('start_time',DateTime, default=datetime.utcnow),
	Column('server', UnicodeText()),
	Column('basepath', UnicodeText()),
        mysql_engine='InnoDB',
)

log_recipe_task_result_table = Table('log_recipe_task_result', metadata,
        Column('id', Integer, primary_key=True),
        Column('recipe_task_result_id', Integer,
                ForeignKey('recipe_task_result.id'), nullable=False),
        Column('path', UnicodeText()),
        Column('filename', UnicodeText(), nullable=False),
        Column('start_time',DateTime, default=datetime.utcnow),
	Column('server', UnicodeText()),
	Column('basepath', UnicodeText()),
        mysql_engine='InnoDB',
)

reservation_table = Table('reservation', metadata,
        Column('id', Integer, primary_key=True),
        Column('system_id', Integer, ForeignKey('system.id'), nullable=False),
        Column('user_id', Integer, ForeignKey('tg_user.user_id'),
            nullable=False),
        Column('start_time', DateTime, index=True, nullable=False,
            default=datetime.utcnow),
        Column('finish_time', DateTime, index=True),
        # type = 'manual' or 'recipe'
        # XXX Use Enum types
        Column('type', Unicode(30), index=True, nullable=False),
        mysql_engine='InnoDB',
)

# this only really exists to make reporting efficient
system_status_duration_table = Table('system_status_duration', metadata,
        Column('id', Integer, primary_key=True),
        Column('system_id', Integer, ForeignKey('system.id'), nullable=False),
        Column('status', SystemStatus.db_type(), nullable=False),
        Column('start_time', DateTime, index=True, nullable=False,
            default=datetime.utcnow),
        Column('finish_time', DateTime, index=True),
        mysql_engine='InnoDB',
)

recipe_table = Table('recipe',metadata,
        Column('id', Integer, primary_key=True),
        Column('recipe_set_id', Integer,
                ForeignKey('recipe_set.id'), nullable=False),
        Column('distro_tree_id', Integer,
                ForeignKey('distro_tree.id')),
        Column('rendered_kickstart_id', Integer, ForeignKey('rendered_kickstart.id',
                name='recipe_rendered_kickstart_id_fk', ondelete='SET NULL')),
        Column('result', TaskResult.db_type(), nullable=False,
                default=TaskResult.new, index=True),
        Column('status', TaskStatus.db_type(), nullable=False,
                default=TaskStatus.new, index=True),
        Column('start_time',DateTime),
        Column('finish_time',DateTime),
        Column('_host_requires',UnicodeText()),
        Column('_distro_requires',UnicodeText()),
        # This column is actually a custom user-supplied kickstart *template*
        # (if not NULL), the generated kickstart for the recipe is defined above
        Column('kickstart',UnicodeText()),
        # type = recipe, machine_recipe or guest_recipe
        Column('type', String(30), nullable=False),
        # Total tasks
	Column('ttasks', Integer, default=0),
        # Total Passing tasks
        Column('ptasks', Integer, default=0),
        # Total Warning tasks
        Column('wtasks', Integer, default=0),
        # Total Failing tasks
        Column('ftasks', Integer, default=0),
        # Total Panic tasks
        Column('ktasks', Integer, default=0),
        Column('whiteboard',Unicode(2000)),
        Column('ks_meta', String(1024)),
        Column('kernel_options', String(1024)),
        Column('kernel_options_post', String(1024)),
        Column('role', Unicode(255)),
        Column('panic', Unicode(20)),
        Column('_partitions',UnicodeText()),
        Column('autopick_random', Boolean, default=False),
        Column('log_server', Unicode(255), index=True),
        Column('virt_status', RecipeVirtStatus.db_type(), index=True,
                nullable=False, default=RecipeVirtStatus.possible),
        mysql_engine='InnoDB',
)

machine_recipe_table = Table('machine_recipe', metadata,
        Column('id', Integer, ForeignKey('recipe.id'), primary_key=True),
        mysql_engine='InnoDB',
)

guest_recipe_table = Table('guest_recipe', metadata,
        Column('id', Integer, ForeignKey('recipe.id'), primary_key=True),
        Column('guestname', UnicodeText()),
        Column('guestargs', UnicodeText()),
        mysql_engine='InnoDB',
)

machine_guest_map =Table('machine_guest_map',metadata,
        Column('machine_recipe_id', Integer,
                ForeignKey('machine_recipe.id', onupdate='CASCADE', ondelete='CASCADE'),
                primary_key=True),
        Column('guest_recipe_id', Integer,
                ForeignKey('recipe.id', onupdate='CASCADE', ondelete='CASCADE'),
                primary_key=True),
        mysql_engine='InnoDB',
)

system_recipe_map = Table('system_recipe_map', metadata,
        Column('system_id', Integer,
                ForeignKey('system.id', onupdate='CASCADE', ondelete='CASCADE'),
                primary_key=True),
        Column('recipe_id', Integer,
                ForeignKey('recipe.id', onupdate='CASCADE', ondelete='CASCADE'),
                primary_key=True),
        mysql_engine='InnoDB',
)

recipe_resource_table = Table('recipe_resource', metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('recipe_id', Integer, ForeignKey('recipe.id',
        name='recipe_resource_recipe_id_fk',
        onupdate='CASCADE', ondelete='CASCADE'),
        nullable=False, unique=True),
    Column('type', ResourceType.db_type(), nullable=False),
    Column('fqdn', Unicode(255), default=None),
    Column('rebooted', DateTime, nullable=True, default=None),
    Column('install_started', DateTime, nullable=True, default=None),
    Column('install_finished', DateTime, nullable=True, default=None),
    Column('postinstall_finished', DateTime, nullable=True, default=None),
    mysql_engine='InnoDB',
)

system_resource_table = Table('system_resource', metadata,
    Column('id', Integer, ForeignKey('recipe_resource.id',
            name='system_resource_id_fk'), primary_key=True),
    Column('system_id', Integer, ForeignKey('system.id',
            name='system_resource_system_id_fk'), nullable=False),
    Column('reservation_id', Integer, ForeignKey('reservation.id',
            name='system_resource_reservation_id_fk')),
    mysql_engine='InnoDB',
)

virt_resource_table = Table('virt_resource', metadata,
    Column('id', Integer, ForeignKey('recipe_resource.id',
            name='virt_resource_id_fk'), primary_key=True),
    Column('system_name', Unicode(2048), nullable=False),
    Column('lab_controller_id', Integer, ForeignKey('lab_controller.id',
            name='virt_resource_lab_controller_id_fk')),
    Column('mac_address', MACAddress(), index=True, default=None),
    mysql_engine='InnoDB',
)

guest_resource_table = Table('guest_resource', metadata,
    Column('id', Integer, ForeignKey('recipe_resource.id',
            name='guest_resource_id_fk'), primary_key=True),
    Column('mac_address', MACAddress(), index=True, default=None),
    mysql_engine='InnoDB',
)

recipe_tag_table = Table('recipe_tag',metadata,
        Column('id', Integer, primary_key=True),
        Column('tag', Unicode(255)),
        mysql_engine='InnoDB',
)

recipe_tag_map = Table('recipe_tag_map', metadata,
        Column('tag_id', Integer,
               ForeignKey('recipe_tag.id', onupdate='CASCADE', ondelete='CASCADE'),
               primary_key=True),
        Column('recipe_id', Integer, 
               ForeignKey('recipe.id', onupdate='CASCADE', ondelete='CASCADE'),
               primary_key=True),
        mysql_engine='InnoDB',
)

recipe_rpm_table =Table('recipe_rpm',metadata,
        Column('id', Integer, primary_key=True),
        Column('recipe_id', Integer,
                ForeignKey('recipe.id'), nullable=False),
        Column('package',Unicode(255)),
        Column('version',Unicode(255)),
        Column('release',Unicode(255)),
        Column('epoch',Integer),
        Column('arch',Unicode(255)),
        Column('running_kernel', Boolean),
        mysql_engine='InnoDB',
)

recipe_repo_table =Table('recipe_repo',metadata,
        Column('id', Integer, primary_key=True),
        Column('recipe_id', Integer,
                ForeignKey('recipe.id'), nullable=False),
        Column('name',Unicode(255)),
        Column('url',Unicode(1024)),
        mysql_engine='InnoDB',
)

recipe_ksappend_table = Table('recipe_ksappend', metadata,
        Column('id', Integer, primary_key=True),
        Column('recipe_id', Integer,
                ForeignKey('recipe.id'), nullable=False),
        Column('ks_append',UnicodeText()),
        mysql_engine='InnoDB',
)

recipe_task_table =Table('recipe_task',metadata,
        Column('id', Integer, primary_key=True),
        Column('recipe_id', Integer, ForeignKey('recipe.id'), nullable=False),
        Column('task_id', Integer, ForeignKey('task.id'), nullable=False),
        Column('start_time',DateTime),
        Column('finish_time',DateTime),
        Column('result', TaskResult.db_type(), nullable=False,
                default=TaskResult.new),
        Column('status', TaskStatus.db_type(), nullable=False,
                default=TaskStatus.new),
        Column('role', Unicode(255)),
        mysql_engine='InnoDB',
)

recipe_task_param_table = Table('recipe_task_param', metadata,
        Column('id', Integer, primary_key=True),
        Column('recipe_task_id', Integer,
                ForeignKey('recipe_task.id')),
        Column('name',Unicode(255)),
        Column('value',UnicodeText()),
        mysql_engine='InnoDB',
)

recipe_task_comment_table = Table('recipe_task_comment',metadata,
        Column('id', Integer, primary_key=True),
        Column('recipe_task_id', Integer,
                ForeignKey('recipe_task.id')),
        Column('comment', UnicodeText()),
        Column('created', DateTime),
        Column('user_id', Integer,
                ForeignKey('tg_user.user_id'), index=True),
        mysql_engine='InnoDB',
)

recipe_task_bugzilla_table = Table('recipe_task_bugzilla',metadata,
        Column('id', Integer, primary_key=True),
        Column('recipe_task_id', Integer,
                ForeignKey('recipe_task.id')),
        Column('bugzilla_id', Integer),
        mysql_engine='InnoDB',
)

recipe_task_rpm_table =Table('recipe_task_rpm',metadata,
        Column('recipe_task_id', Integer,
                ForeignKey('recipe_task.id'), primary_key=True),
        Column('package',Unicode(255)),
        Column('version',Unicode(255)),
        Column('release',Unicode(255)),
        Column('epoch',Integer),
        Column('arch',Unicode(255)),
        Column('running_kernel', Boolean),
        mysql_engine='InnoDB',
)

recipe_task_result_table = Table('recipe_task_result',metadata,
        Column('id', Integer, primary_key=True),
        Column('recipe_task_id', Integer,
                ForeignKey('recipe_task.id')),
        Column('path', Unicode(2048)),
        Column('result', TaskResult.db_type(), nullable=False,
                default=TaskResult.new),
        Column('score', Numeric(10)),
        Column('log', UnicodeText()),
        Column('start_time',DateTime, default=datetime.utcnow),
        mysql_engine='InnoDB',
)

# This is for storing final generated kickstarts to be provisioned,
# not user-supplied kickstart templates or anything else like that.
rendered_kickstart_table = Table('rendered_kickstart', metadata,
    Column('id', Integer, primary_key=True),
    # Either kickstart or url should be populated -- if url is present,
    # it means fetch the kickstart from there instead
    Column('kickstart', UnicodeText),
    Column('url', UnicodeText),
    mysql_engine='InnoDB',
)

task_table = Table('task',metadata,
        Column('id', Integer, primary_key=True),
        Column('name', Unicode(255), unique=True),
        Column('rpm', Unicode(255), unique=True),
        Column('path', Unicode(4096)),
        Column('description', Unicode(2048)),
        Column('repo', Unicode(256)),
        Column('avg_time', Integer, default=0),
        Column('destructive', Boolean),
        Column('nda', Boolean),
        # This should be a map table
        #Column('notify', Unicode(2048)),

        Column('creation_date', DateTime, default=datetime.utcnow),
        Column('update_date', DateTime, onupdate=datetime.utcnow),
        Column('uploader_id', Integer, ForeignKey('tg_user.user_id')),
        Column('owner', Unicode(255), index=True),
        Column('version', Unicode(256)),
        Column('license', Unicode(256)),
        Column('priority', Unicode(256)),
        Column('valid', Boolean, default=True),
        mysql_engine='InnoDB',
)

task_bugzilla_table = Table('task_bugzilla',metadata,
        Column('id', Integer, primary_key=True),
        Column('bugzilla_id', Integer),
        Column('task_id', Integer,
                ForeignKey('task.id')),
        mysql_engine='InnoDB',
)

task_packages_runfor_map = Table('task_packages_runfor_map', metadata,
    Column('task_id', Integer, ForeignKey('task.id', onupdate='CASCADE',
        ondelete='CASCADE'), primary_key=True),
    Column('package_id', Integer, ForeignKey('task_package.id',
        onupdate='CASCADE', ondelete='CASCADE'), primary_key=True),
    mysql_engine='InnoDB',
)

task_packages_required_map = Table('task_packages_required_map', metadata,
    Column('task_id', Integer, ForeignKey('task.id', onupdate='CASCADE',
        ondelete='CASCADE'), primary_key=True),
    Column('package_id', Integer, ForeignKey('task_package.id',
        onupdate='CASCADE', ondelete='CASCADE'), primary_key=True),
    mysql_engine='InnoDB',
)

task_packages_custom_map = Table('task_packages_custom_map', metadata,
    Column('recipe_id', Integer, ForeignKey('recipe.id', onupdate='CASCADE',
        ondelete='CASCADE'), primary_key=True),
    Column('package_id', Integer, ForeignKey('task_package.id',
        onupdate='CASCADE', ondelete='CASCADE'), primary_key=True),
    mysql_engine='InnoDB',
)

task_property_needed_table = Table('task_property_needed', metadata,
        Column('id', Integer, primary_key=True),
        Column('task_id', Integer,
                ForeignKey('task.id')),
        Column('property', Unicode(2048)),
        mysql_engine='InnoDB',
)

task_package_table = Table('task_package',metadata,
        Column('id', Integer, primary_key=True),
        Column('package', Unicode(255), nullable=False, unique=True),
        mysql_engine='InnoDB',
        mysql_collate='utf8_bin',
)

task_type_table = Table('task_type',metadata,
        Column('id', Integer, primary_key=True),
        Column('type', Unicode(255), nullable=False, unique=True),
        mysql_engine='InnoDB',
)

task_type_map = Table('task_type_map',metadata,
    Column('task_id', Integer, ForeignKey('task.id', onupdate='CASCADE',
        ondelete='CASCADE'), primary_key=True),
    Column('task_type_id', Integer, ForeignKey('task_type.id',
        onupdate='CASCADE', ondelete='CASCADE'), primary_key=True),
    mysql_engine='InnoDB',
)

class System(SystemObject, ActivityMixin):

    @property
    def activity_type(self):
        return SystemActivity

    def __init__(self, fqdn=None, status=SystemStatus.broken, contact=None, location=None,
                       model=None, type=SystemType.machine, serial=None, vendor=None,
                       owner=None, lab_controller=None, lender=None,
                       hypervisor=None, loaned=None, memory=None,
                       kernel_type=None, cpu=None):
        super(System, self).__init__()
        self.fqdn = fqdn
        self.status = status
        self.contact = contact
        self.location = location
        self.model = model
        self.type = type
        self.serial = serial
        self.vendor = vendor
        self.owner = owner
        self.lab_controller = lab_controller
        self.lender = lender
        self.hypervisor = hypervisor
        self.loaned = loaned
        self.memory = memory
        self.kernel_type = kernel_type
        self.cpu = cpu

    def to_xml(self, clone=False):
        """ Return xml describing this system """
        fields = dict(
                      hostname    = 'fqdn',
                      system_type = 'type',
                     )

        host_requires = xmldoc.createElement('hostRequires')
        xmland = xmldoc.createElement('and')
        for key in fields.keys():
            require = xmldoc.createElement(key)
            require.setAttribute('op', '=')
            value = getattr(self, fields[key], None) or u''
            require.setAttribute('value', unicode(value))
            xmland.appendChild(require)
        host_requires.appendChild(xmland)
        return host_requires

    @classmethod
    def all(cls, user=None, system=None): 
        """
        Only systems that the current user has permission to see
        
        """
        if system is None:
            system = cls.query
        return cls.permissable_systems(query=system, user=user)

    @classmethod
    def permissable_systems(cls, query, user=None, *arg, **kw):

        if user is None:
            try:
                user = identity.current.user
            except AttributeError:
                user = None

        if user:
            if not user.is_admin() and \
               not user.has_permission(u'secret_visible'):
                query = query.filter(
                            or_(System.private==False,
                                System.owner == user,
                                System.loaned == user,
                                System.user == user))
        else:
            query = query.filter(System.private==False)
         
        return query


    @classmethod
    def free(cls, user, systems=None):
        """
        Builds on available.  Only systems with no users, and not Loaned.
        """
        return System.available(user,systems).\
            filter(and_(System.user==None, or_(System.loaned==None, System.loaned==user))). \
            join(System.lab_controller).filter(LabController.disabled==False)

    @classmethod
    def available_for_schedule(cls, user, systems=None):
        """ 
        Will return systems that are available to user for scheduling
        """
        return cls._available(user, systems=systems, system_status=SystemStatus.automated)

    @classmethod
    def _available(self, user, system_status=None, systems=None):
        """
        Builds on all.  Only systems which this user has permission to reserve.
        Can take varying system_status' as args as well
        """

        query = System.all(user, system=systems)
        if system_status is None:
            query = query.filter(or_(System.status==SystemStatus.automated,
                    System.status==SystemStatus.manual))
        elif isinstance(system_status, list):
            query = query.filter(or_(*[System.status==k for k in system_status]))
        else:
            query = query.filter(System.status==system_status)

        # these filter conditions correspond to can_reserve
        query = query.outerjoin(System.custom_access_policy).filter(or_(
                System.owner == user,
                System.loaned == user,
                SystemAccessPolicy.grants(user, SystemPermission.reserve)))
        return query


    @classmethod
    def available(cls, user, systems=None):
        """
        Will return systems that are available to user
        """
        return cls._available(user, systems=systems)

    @classmethod
    def scheduler_ordering(cls, user, query):
        # Order by:
        #   System Owner
        #   System group
        #   Single procesor bare metal system
        return query.outerjoin(System.cpu).order_by(
            case([(System.owner==user, 1),
                (and_(System.owner!=user, System.group_assocs != None), 2)],
                else_=3),
                and_(System.hypervisor == None, Cpu.processors == 1))

    @classmethod
    def mine(cls, user):
        """
        A class method that can be used to search for systems that only
        user can see
        """
        return cls.query.filter(or_(System.user==user,
                                    System.loaned==user))

    @classmethod
    def by_fqdn(cls, fqdn, user):
        """
        A class method that can be used to search systems
        based on the fqdn since it is unique.
        """
        return System.all(user).filter(System.fqdn == fqdn).one()

    @classmethod
    def list_by_fqdn(cls, fqdn, user):
        """
        A class method that can be used to search systems
        based on the fqdn since it is unique.
        """
        return System.all(user).filter(System.fqdn.like('%s%%' % fqdn))

    @classmethod
    def by_id(cls, id, user):
        return System.all(user).filter(System.id == id).one()

    @classmethod
    def by_group(cls,group_id,*args,**kw):
        return System.query.join(SystemGroup,Group).filter(Group.group_id == group_id)

    @classmethod
    def by_type(cls,type,user=None,systems=None):
        if systems:
            query = systems
        else:
            if user:
                query = System.all(user)
            else:
                query = System.all()
        return query.filter(System.type == type)

    @classmethod
    def by_arch(cls,arch,query=None):
        if query:
            return query.filter(System.arch.any(Arch.arch == arch))
        else:
            return System.query.filter(System.arch.any(Arch.arch == arch))

    def has_manual_reservation(self, user):
        """Does the specified user currently have a manual reservation?"""
        reservation = self.open_reservation
        return (reservation and reservation.type == u'manual' and
                user and self.user == user)

    def unreserve_manually_reserved(self, *args, **kw):
        open_reservation = self.open_reservation
        if not open_reservation:
            raise BX(_(u'System %s is not currently reserved' % self.fqdn))
        reservation_type = open_reservation.type
        if reservation_type == 'recipe':
            recipe_id = open_reservation.recipe.id
            raise BX(_(u'Currently running R:%s' % recipe_id))
        self.unreserve(reservation=open_reservation, *args, **kw)

    def excluded_families(self):
        """
        massage excluded_osmajor for Checkbox values
        """
        major = {}
        version = {}
        for arch in self.arch:
            major[arch.arch] = [osmajor.osmajor.id for osmajor in self.excluded_osmajor_byarch(arch)]
            version[arch.arch] = [osversion.osversion.id for osversion in self.excluded_osversion_byarch(arch)]

        return (major,version)
    excluded_families=property(excluded_families)

    def install_options(self, distro_tree):
        """
        Return install options based on distro selected.
        Inherit options from Arch -> Family -> Update
        """
        osmajor = distro_tree.distro.osversion.osmajor
        result = global_install_options()
        # arch=None means apply to all arches
        if None in osmajor.install_options_by_arch:
            op = osmajor.install_options_by_arch[None]
            op_opts = InstallOptions.from_strings(op.ks_meta, op.kernel_options,
                    op.kernel_options_post)
            result = result.combined_with(op_opts)
        if distro_tree.arch in osmajor.install_options_by_arch:
            opa = osmajor.install_options_by_arch[distro_tree.arch]
            opa_opts = InstallOptions.from_strings(opa.ks_meta, opa.kernel_options,
                    opa.kernel_options_post)
            result = result.combined_with(opa_opts)
        result = result.combined_with(distro_tree.install_options())
        if distro_tree.arch in self.provisions:
            pa = self.provisions[distro_tree.arch]
            pa_opts = InstallOptions.from_strings(pa.ks_meta, pa.kernel_options,
                    pa.kernel_options_post)
            result = result.combined_with(pa_opts)
            if distro_tree.distro.osversion.osmajor in pa.provision_families:
                pf = pa.provision_families[distro_tree.distro.osversion.osmajor]
                pf_opts = InstallOptions.from_strings(pf.ks_meta,
                        pf.kernel_options, pf.kernel_options_post)
                result = result.combined_with(pf_opts)
                if distro_tree.distro.osversion in pf.provision_family_updates:
                    pfu = pf.provision_family_updates[distro_tree.distro.osversion]
                    pfu_opts = InstallOptions.from_strings(pfu.ks_meta,
                            pfu.kernel_options, pfu.kernel_options_post)
                    result = result.combined_with(pfu_opts)
        return result

    def is_free(self):
        try:
            user = identity.current.user
        except Exception:
            user = None

        if not self.user and (not self.loaned or self.loaned == user):
            return True
        else:
            return False

    def can_change_owner(self, user):
        """
        Does the given user have permission to change the owner of this system?
        """
        if self.owner == user:
            return True
        if user.is_admin():
            return True
        return False

    def can_edit_policy(self, user):
        """
        Does the given user have permission to edit this system's access policy?
        """
        if self.owner == user:
            return True
        if user.is_admin():
            return True
        if (self.custom_access_policy and
            self.custom_access_policy.grants(user, SystemPermission.edit_policy)):
            return True
        return False

    def can_edit(self, user):
        """
        Does the given user have permission to edit details (inventory info, 
        power config, etc) of this system?
        """
        if self.owner == user:
            return True
        if user.is_admin():
            return True
        if (self.custom_access_policy and
            self.custom_access_policy.grants(user, SystemPermission.edit_system)):
            return True
        return False

    def can_lend(self, user):
        """
        Does the given user have permission to loan this system to another user?
        """
        # System owner is always a loan admin
        if self.owner == user:
            return True
        # Beaker instance admins are loan admins for every system
        if user.is_admin():
            return True
        # Anyone else needs the "loan_any" permission
        if (self.custom_access_policy and
            self.custom_access_policy.grants(user, SystemPermission.loan_any)):
            return True
        return False

    def can_borrow(self, user):
        """
        Does the given user have permission to loan this system to themselves?
        """
        # Loan admins can always loan to themselves
        if self.can_lend(user):
            return True
        # "loan_self" only lets you take an unloaned system and update the
        # details on a loan already granted to you
        if ((not self.loaned or self.loaned == user) and
                self.custom_access_policy and
                self.custom_access_policy.grants(user,
                                                 SystemPermission.loan_self)):
            return True
        return False

    def can_return_loan(self, user):
        """
        Does the given user have permission to cancel the current loan for this 
        system?
        """
        # Users can always return their own loans
        if self.loaned and self.loaned == user:
            return True
        # Loan admins can return anyone's loan
        return self.can_lend(user)

    def can_reserve(self, user):
        """
        Does the given user have permission to reserve this system?

        Note that if is_free() returns False, the user may still not be able
        to reserve it *right now*.
        """
        # System owner can always reserve the system
        if self.owner == user:
            return True
        # Loans grant the ability to reserve the system
        if self.loaned and self.loaned == user:
            return True
        # Anyone else needs the "reserve" permission
        if (self.custom_access_policy and
            self.custom_access_policy.grants(user, SystemPermission.reserve)):
            return True
        # Beaker admins can effectively reserve any system, but need to
        # grant themselves the appropriate permissions first (or loan the
        # system to themselves)
        return False

    def can_reserve_manually(self, user):
        """
        Does the given user have permission to manually reserve this system?
        """
        # Manual reservations are permitted only for systems that are
        # either not automated or are currently loaned to this user
        if (self.status != SystemStatus.automated or
              (self.loaned and self.loaned == user)):
            return self.can_reserve(user)
        return False

    def can_unreserve(self, user):
        """
        Does the given user have permission to return the current reservation 
        on this system?
        """
        # Users can always return their own reservations
        if self.user and self.user == user:
            return True
        # Loan admins can return anyone's reservation
        return self.can_lend(user)

    def can_power(self, user):
        """
        Does the given user have permission to run power/netboot commands on 
        this system?
        """
        # Current user can always control the system
        if self.user and self.user == user:
            return True
        # System owner can always control the system
        if self.owner == user:
            return True
        # Beaker admins can control any system
        if user.is_admin():
            return True
        # Anyone else needs the "control_system" permission
        if (self.custom_access_policy and
            self.custom_access_policy.grants(user, SystemPermission.control_system)):
            return True
        return False

    def get_loan_details(self):
        """Returns details of the loan as a dict"""
        if not self.loaned:
            return {}
        return {
                   "recipient": self.loaned.user_name,
                   "comment": self.loan_comment,
               }

    def grant_loan(self, recipient, comment, service):
        """Grants a loan to the designated user if permitted"""
        if recipient is None:
            recipient = identity.current.user.user_name
        self.change_loan(recipient, comment, service)

    def return_loan(self, service):
        """Grants a loan to the designated user if permitted"""
        self.change_loan(None, None, service)

    def change_loan(self, user_name, comment=None, service='WEBUI'):
        """Changes the current system loan

        change_loan() updates the user a system is loaned to, by
        either adding a new loanee, changing the existing to another,
        or by removing the existing loanee. It also changes the comment
        associated with the loan.

        It checks all permissions that are needed and
        updates SystemActivity.

        Returns the name of the user now holding the loan (if any), otherwise
        returns the empty string.
        """
        loaning_to = user_name
        if loaning_to:
            user = User.by_user_name(loaning_to)
            if not user:
                # This is an error condition
                raise ValueError('user name %s is invalid' % loaning_to)
            if user == identity.current.user:
                if not self.can_borrow(identity.current.user):
                    msg = '%s cannot borrow this system' % user
                    raise InsufficientSystemPermissions(msg)
            else:
                if not self.can_lend(identity.current.user):
                    msg = ('%s cannot lend this system to %s' %
                                           (identity.current.user, user))
                    raise InsufficientSystemPermissions(msg)
        else:
            if not self.can_return_loan(identity.current.user):
                msg = '%s cannot return system loan' % identity.current.user
                raise InsufficientSystemPermissions(msg)
            user = None
            comment = None

        if user != self.loaned:
            activity = SystemActivity(identity.current.user, service,
                u'Changed', u'Loaned To',
                u'%s' % self.loaned if self.loaned else '',
                u'%s' % user if user else '')
            self.loaned = user
            self.activity.append(activity)

        if self.loan_comment != comment:
            activity = SystemActivity(identity.current.user, service,
                u'Changed', u'Loan Comment', u'%s' % self.loan_comment if
                self.loan_comment else '' , u'%s' % comment if
                comment else '')
            self.activity.append(activity)
            self.loan_comment = comment

        return loaning_to if loaning_to else ''

    ALLOWED_ATTRS = ['vendor', 'model', 'memory'] #: attributes which the inventory scripts may set
    PRESERVED_ATTRS = ['vendor', 'model'] #: attributes which should only be set when empty

    def get_update_method(self,obj_str):
        methods = dict ( Cpu = self.updateCpu, Arch = self.updateArch, 
                         Devices = self.updateDevices, Numa = self.updateNuma,
                         Hypervisor = self.updateHypervisor, Disk = self.updateDisk)
        return methods[obj_str]

    def update_legacy(self, inventory):
        """
        Update Key/Value pairs for legacy RHTS
        """
        keys_to_update = set()
        new_int_kvs = set()
        new_string_kvs = set()
        for key_name, values in inventory.items():
            try:
                key = Key.by_name(key_name)
            except InvalidRequestError:
                continue
            keys_to_update.add(key)
            if not isinstance(values, list):
                values = [values]
            for value in values:
                if isinstance(value, bool):
                    # MySQL will int-ify these, so we do it here 
                    # to make our comparisons accurate
                    value = int(value)
                if key.numeric:
                    new_int_kvs.add((key, int(value)))
                else:
                    new_string_kvs.add((key, unicode(value)))

        # Examine existing key-values to find what we already have, and what 
        # needs to be removed
        for kv in list(self.key_values_int):
            if kv.key in keys_to_update:
                if (kv.key, kv.key_value) in new_int_kvs:
                    new_int_kvs.remove((kv.key, kv.key_value))
                else:
                    self.key_values_int.remove(kv)
                    self.activity.append(SystemActivity(user=identity.current.user,
                            service=u'XMLRPC', action=u'Removed', field_name=u'Key/Value',
                            old_value=u'%s/%s' % (kv.key.key_name, kv.key_value),
                            new_value=None))
        for kv in list(self.key_values_string):
            if kv.key in keys_to_update:
                if (kv.key, kv.key_value) in new_string_kvs:
                    new_string_kvs.remove((kv.key, kv.key_value))
                else:
                    self.key_values_string.remove(kv)
                    self.activity.append(SystemActivity(user=identity.current.user,
                            service=u'XMLRPC', action=u'Removed', field_name=u'Key/Value',
                            old_value=u'%s/%s' % (kv.key.key_name, kv.key_value),
                            new_value=None))

        # Now we can just add the new ones
        for key, value in new_int_kvs:
            self.key_values_int.append(Key_Value_Int(key, value))
            self.activity.append(SystemActivity(user=identity.current.user,
                    service=u'XMLRPC', action=u'Added',
                    field_name=u'Key/Value', old_value=None,
                    new_value=u'%s/%s' % (key.key_name, value)))
        for key, value in new_string_kvs:
            self.key_values_string.append(Key_Value_String(key, value))
            self.activity.append(SystemActivity(user=identity.current.user,
                    service=u'XMLRPC', action=u'Added',
                    field_name=u'Key/Value', old_value=None,
                    new_value=u'%s/%s' % (key.key_name, value)))

        self.date_modified = datetime.utcnow()
        return 0
                    

    def update(self, inventory):
        """ Update Inventory """

        # Update last checkin even if we don't change anything.
        self.date_lastcheckin = datetime.utcnow()

        md5sum = md5("%s" % inventory).hexdigest()
        if self.checksum == md5sum:
            return 0
        self.activity.append(SystemActivity(user=identity.current.user,
                service=u'XMLRPC', action=u'Changed', field_name=u'checksum',
                old_value=self.checksum, new_value=md5sum))
        self.checksum = md5sum
        for key in inventory:
            if key in self.ALLOWED_ATTRS:
                if key in self.PRESERVED_ATTRS and getattr(self, key, None):
                    continue
                setattr(self, key, inventory[key])
                self.activity.append(SystemActivity(
                        user=identity.current.user,
                        service=u'XMLRPC', action=u'Changed',
                        field_name=key, old_value=None,
                        new_value=inventory[key]))
            else:
                try:
                    method = self.get_update_method(key)
                except KeyError:
                    log.warning('Attempted to update unknown inventory property \'%s\' on %s' %
                                (key, self.fqdn))
                else:
                    method(inventory[key])
        self.date_modified = datetime.utcnow()
        return 0

    def updateHypervisor(self, hypervisor):
        if hypervisor:
            try:
                hvisor = Hypervisor.by_name(hypervisor)
            except InvalidRequestError:
                raise BX(_('Invalid Hypervisor: %s' % hypervisor))
        else:
            hvisor = None
        if self.hypervisor != hvisor:
            self.activity.append(SystemActivity(
                    user=identity.current.user,
                    service=u'XMLRPC', action=u'Changed',
                    field_name=u'Hypervisor', old_value=self.hypervisor,
                    new_value=hvisor))
            self.hypervisor = hvisor

    def updateArch(self, archinfo):
        for arch in archinfo:
            try:
                new_arch = Arch.by_name(arch)
            except NoResultFound:
                new_arch = Arch(arch=arch)
            if new_arch not in self.arch:
                self.arch.append(new_arch)
                self.activity.append(SystemActivity(
                        user=identity.current.user,
                        service=u'XMLRPC', action=u'Added',
                        field_name=u'Arch', old_value=None,
                        new_value=new_arch.arch))

    def updateDisk(self, diskinfo):
        currentDisks = []
        self.disks = getattr(self, 'disks', [])

        for disk in diskinfo['Disks']:
            disk = Disk(**disk)
            if disk not in self.disks:
                self.disks.append(disk)
                self.activity.append(SystemActivity(
                        user=identity.current.user,
                        service=u'XMLRPC', action=u'Added',
                        field_name=u'Disk', old_value=None,
                        new_value=disk.size))
            currentDisks.append(disk)

        for disk in self.disks:
            if disk not in currentDisks:
                self.disks.remove(disk)
                self.activity.append(SystemActivity(
                        user=identity.current.user,
                        service=u'XMLRPC', action=u'Removed',
                        field_name=u'Disk', old_value=disk.size,
                        new_value=None))

    def updateDevices(self, deviceinfo):
        currentDevices = []
        for device in deviceinfo:
            device_class = DeviceClass.lazy_create(device_class=device['type'])
            mydevice = Device.lazy_create(vendor_id = device['vendorID'],
                                   device_id = device['deviceID'],
                                   subsys_vendor_id = device['subsysVendorID'],
                                   subsys_device_id = device['subsysDeviceID'],
                                   bus = device['bus'],
                                   driver = device['driver'],
                                   device_class_id = device_class.id,
                                   description = device['description'])
            if mydevice not in self.devices:
                self.devices.append(mydevice)
                self.activity.append(SystemActivity(
                        user=identity.current.user,
                        service=u'XMLRPC', action=u'Added',
                        field_name=u'Device', old_value=None,
                        new_value=mydevice.id))
            currentDevices.append(mydevice)
        # Remove any old entries
        for device in self.devices[:]:
            if device not in currentDevices:
                self.devices.remove(device)
                self.activity.append(SystemActivity(
                        user=identity.current.user,
                        service=u'XMLRPC', action=u'Removed',
                        field_name=u'Device', old_value=device.id,
                        new_value=None))

    def updateCpu(self, cpuinfo):
        # Remove all old CPU data
        if self.cpu:
            for flag in self.cpu.flags:
                session.delete(flag)
            session.delete(self.cpu)

        # Create new Cpu
        cpu = Cpu(vendor     = cpuinfo['vendor'],
                  model      = cpuinfo['model'],
                  model_name = cpuinfo['modelName'],
                  family     = cpuinfo['family'],
                  stepping   = cpuinfo['stepping'],
                  speed      = cpuinfo['speed'],
                  processors = cpuinfo['processors'],
                  cores      = cpuinfo['cores'],
                  sockets    = cpuinfo['sockets'],
                  flags      = cpuinfo['CpuFlags'])

        self.cpu = cpu
        self.activity.append(SystemActivity(
                user=identity.current.user,
                service=u'XMLRPC', action=u'Changed',
                field_name=u'CPU', old_value=None,
                new_value=None)) # XXX find a good way to record the actual changes

    def updateNuma(self, numainfo):
        if self.numa:
            session.delete(self.numa)
        if numainfo.get('nodes', None) is not None:
            self.numa = Numa(nodes=numainfo['nodes'])
        self.activity.append(SystemActivity(
                user=identity.current.user,
                service=u'XMLRPC', action=u'Changed',
                field_name=u'NUMA', old_value=None,
                new_value=None)) # XXX find a good way to record the actual changes

    def excluded_osmajor_byarch(self, arch):
        """
        List excluded osmajor for system by arch
        """
        excluded = ExcludeOSMajor.query.join('system').\
                    join('arch').filter(and_(System.id==self.id,
                                             Arch.id==arch.id))
        return excluded

    def excluded_osversion_byarch(self, arch):
        """
        List excluded osversion for system by arch
        """
        excluded = ExcludeOSVersion.query.join('system').\
                    join('arch').filter(and_(System.id==self.id,
                                             Arch.id==arch.id))
        return excluded

    def distro_trees(self, only_in_lab=True):
        """
        List of distro trees that support this system
        """
        query = DistroTree.query\
                .join(DistroTree.distro, Distro.osversion, OSVersion.osmajor)\
                .options(contains_eager(DistroTree.distro, Distro.osversion, OSVersion.osmajor))
        if only_in_lab:
            query = query.filter(DistroTree.lab_controller_assocs.any(
                    LabControllerDistroTree.lab_controller == self.lab_controller))
        else:
            query = query.filter(DistroTree.lab_controller_assocs.any())
        query = query.filter(DistroTree.arch_id.in_([a.id for a in self.arch]))\
                .filter(not_(OSMajor.excluded_osmajors.any(and_(
                    ExcludeOSMajor.system == self,
                    ExcludeOSMajor.arch_id == DistroTree.arch_id))
                    .correlate(distro_tree_table)))\
                .filter(not_(OSVersion.excluded_osversions.any(and_(
                    ExcludeOSVersion.system == self,
                    ExcludeOSVersion.arch_id == DistroTree.arch_id))
                    .correlate(distro_tree_table)))
        return query

    def action_release(self, service=u'Scheduler'):
        # Attempt to remove Netboot entry and turn off machine
        self.clear_netboot(service=service)
        if self.release_action:
            if self.release_action == ReleaseAction.power_off:
                self.action_power(action=u'off', service=service)
            elif self.release_action == ReleaseAction.leave_on:
                self.action_power(action=u'on', service=service)
            elif self.release_action == ReleaseAction.reprovision:
                if self.reprovision_distro_tree:
                    # There are plenty of things that can go wrong here if the 
                    # system or distro tree is misconfigured. But we don't want 
                    # that to prevent the recipe from being stopped, so we log 
                    # and ignore any errors.
                    try:
                        from bkr.server.kickstart import generate_kickstart
                        install_options = self.install_options(self.reprovision_distro_tree)
                        if 'ks' not in install_options.kernel_options:
                            rendered_kickstart = generate_kickstart(install_options,
                                    distro_tree=self.reprovision_distro_tree,
                                    system=self, user=self.owner)
                            install_options.kernel_options['ks'] = rendered_kickstart.link
                        self.configure_netboot(self.reprovision_distro_tree,
                                install_options.kernel_options_str,
                                service=service)
                        self.action_power(action=u'reboot', service=service)
                    except Exception:
                        log.exception('Failed to re-provision %s on %s, ignoring',
                                self.reprovision_distro_tree, self)
            else:
                raise ValueError('Not a valid ReleaseAction: %r' % self.release_action)
        # Default is to power off, if we can
        elif self.power:
            self.action_power(action=u'off', service=service)

    def configure_netboot(self, distro_tree, kernel_options, service=u'Scheduler',
            callback=None):
        try:
            user = identity.current.user
        except Exception:
            user = None
        if self.lab_controller:
            self.command_queue.append(CommandActivity(user=user,
                    service=service, action=u'clear_logs',
                    status=CommandStatus.queued, callback=callback))
            command = CommandActivity(user=user,
                    service=service, action=u'configure_netboot',
                    status=CommandStatus.queued, callback=callback)
            command.distro_tree = distro_tree
            command.kernel_options = kernel_options
            self.command_queue.append(command)
        else:
            return False

    def action_power(self, action=u'reboot', service=u'Scheduler',
            callback=None, delay=0):
        try:
            user = identity.current.user
        except Exception:
            user = None

        if self.lab_controller and self.power:
            status = CommandStatus.queued
            activity = CommandActivity(user, service, action, status, callback)
            if delay:
                activity.delay_until = datetime.utcnow() + timedelta(seconds=delay)
            self.command_queue.append(activity)
            return activity
        else:
            return False

    def clear_netboot(self, service=u'Scheduler'):
        try:
            user = identity.current.user
        except Exception:
            user = None
        if self.lab_controller:
            self.command_queue.append(CommandActivity(user=user,
                    service=service, action=u'clear_netboot',
                    status=CommandStatus.queued))

    def __repr__(self):
        return self.fqdn

    @property
    def href(self):
        """Returns a relative URL for this system's page."""
        return urllib.quote((u'/view/%s' % self.fqdn).encode('utf8'))

    def link(self):
        """ Return a link to this system
        """
        return make_link(url = '/view/%s' % self.fqdn,
                         text = self.fqdn)

    link = property(link)

    def report_problem_href(self, **kwargs):
        return url('/report_problem', system_id=self.id, **kwargs)

    def mark_broken(self, reason, recipe=None, service=u'Scheduler'):
        """Sets the system status to Broken and notifies its owner."""
        try:
            user = identity.current.user
        except Exception:
            user = None
        log.warning('Marking system %s as broken' % self.fqdn)
        sa = SystemActivity(user, service, u'Changed', u'Status', unicode(self.status), u'Broken')
        self.activity.append(sa)
        self.status = SystemStatus.broken
        self.date_modified = datetime.utcnow()
        mail.broken_system_notify(self, reason, recipe)

    def suspicious_abort(self):
        if self.status == SystemStatus.broken:
            return # nothing to do
        if self.type != SystemType.machine:
            return # prototypes get more leeway, and virtual machines can't really "break"...
        reliable_distro_tag = get('beaker.reliable_distro_tag', None)
        if not reliable_distro_tag:
            return
        # Since its last status change, has this system had an 
        # uninterrupted run of aborted recipes leading up to this one, with 
        # at least two different STABLE distros?
        # XXX this query is stupidly big, I need to do something about it
        session.flush()
        status_change_subquery = session.query(func.max(SystemActivity.created))\
            .filter(and_(
                SystemActivity.system_id == self.id,
                SystemActivity.field_name == u'Status',
                SystemActivity.action == u'Changed'))\
            .subquery()
        nonaborted_recipe_subquery = self.dyn_recipes\
            .filter(Recipe.status != TaskStatus.aborted)\
            .with_entities(func.max(Recipe.finish_time))\
            .subquery()
        count = self.dyn_recipes.join(Recipe.distro_tree, DistroTree.distro)\
            .filter(and_(
                Distro.tags.contains(reliable_distro_tag.decode('utf8')),
                Recipe.start_time >
                    func.ifnull(status_change_subquery.as_scalar(), self.date_added),
                Recipe.finish_time > nonaborted_recipe_subquery.as_scalar().correlate(None)))\
            .value(func.count(DistroTree.id.distinct()))
        if count >= 2:
            # Broken!
            metrics.increment('counters.suspicious_aborts')
            reason = unicode(_(u'System has a run of aborted recipes ' 
                    'with reliable distros'))
            log.warn(reason)
            self.mark_broken(reason=reason)

    def reserve_manually(self, service, user=None):
        if user is None:
            user = identity.current.user
        self._check_can_reserve(user)
        if not self.can_reserve_manually(user):
            raise BX(_(u'Cannot manually reserve automated system, '
                    'without borrowing it first. Schedule a job instead'))
        return self._reserve(service, user, u'manual')

    def reserve_for_recipe(self, service, user=None):
        if user is None:
            user = identity.current.user
        self._check_can_reserve(user)
        return self._reserve(service, user, u'recipe')

    def _check_can_reserve(self, user):
        # Throw an exception if the given user can't reserve the system.
        if self.user is not None and self.user == user:
            raise StaleSystemUserException(_(u'User %s has already reserved '
                'system %s') % (user, self))
        if not self.can_reserve(user):
            raise InsufficientSystemPermissions(_(u'User %s cannot '
                'reserve system %s') % (user, self))
        if self.loaned:
            # loans give exclusive rights to reserve
            if user != self.loaned and user != self.owner:
                raise InsufficientSystemPermissions(_(u'User %s cannot reserve '
                        'system %s while it is loaned to user %s')
                        % (user, self, self.loaned))

    def _reserve(self, service, user, reservation_type):
        # Atomic operation to reserve the system
        session.flush()
        if session.connection(System).execute(system_table.update(
                and_(system_table.c.id == self.id,
                     system_table.c.user_id == None)),
                user_id=user.user_id).rowcount != 1:
            raise StaleSystemUserException(_(u'System %r is already '
                'reserved') % self)
        self.user = user # do it here too, so that the ORM is aware
        reservation = Reservation(user=user, type=reservation_type)
        self.reservations.append(reservation)
        self.activity.append(SystemActivity(user=user,
                service=service, action=u'Reserved', field_name=u'User',
                old_value=u'', new_value=user.user_name))
        log.debug('Created reservation for system %r with type %r, service %r, user %r',
                self, reservation_type, service, user)
        return reservation

    def unreserve(self, service=None, reservation=None, user=None):
        if user is None:
            user = identity.current.user

        if self.user is None:
            raise BX(_(u'System is not reserved'))
        if not self.can_unreserve(user):
            raise InsufficientSystemPermissions(
                    _(u'User %s cannot unreserve system %s, reserved by %s')
                    % (user, self, self.user))

        # Update reservation atomically first, to avoid races
        session.flush()
        my_reservation_id = reservation.id
        if session.connection(System).execute(reservation_table.update(
                and_(reservation_table.c.id == my_reservation_id,
                     reservation_table.c.finish_time == None)),
                finish_time=datetime.utcnow()).rowcount != 1:
            raise BX(_(u'System does not have an open reservation'))
        session.expire(reservation, ['finish_time'])
        old_user = self.user
        self.user = None
        self.action_release(service=service)
        activity = SystemActivity(user=user,
                service=service, action=u'Returned', field_name=u'User',
                old_value=old_user.user_name, new_value=u'')
        self.activity.append(activity)

    def add_note(self, text, user, service=u'WEBUI'):
        note = Note(user=user, text=text)
        self.notes.append(note)
        self.record_activity(user=user, service=service,
                             action='Added', field='Note',
                             old='', new=text)
        self.date_modified = datetime.utcnow()

    cc = association_proxy('_system_ccs', 'email_address')

    groups = association_proxy('group_assocs', 'group',
            creator=lambda group: SystemGroup(group=group))

class SystemStatusAttributeExtension(AttributeExtension):

    def set(self, state, child, oldchild, initiator):
        obj = state.obj()
        log.debug('%r status changed from %r to %r', obj, oldchild, child)
        if child == oldchild:
            return child
        if oldchild in (None, NEVER_SET):
            # First time system.status has been set, there will be no duration 
            # rows yet.
            assert not obj.status_durations
            obj.status_durations.insert(0, SystemStatusDuration(status=child))
            return child
        # Otherwise, there should be exactly one "open" duration row, 
        # with NULL finish_time.
        open_sd = obj.status_durations[0]
        assert open_sd.finish_time is None
        assert open_sd.status == oldchild
        if open_sd in session.new:
            # The current open row is not actually persisted yet. This 
            # happens when system.status is set more than once in 
            # a session. In this case we can just update the same row and 
            # return, no reason to insert another.
            open_sd.status = child
            return child
        # Need to close the open row using a conditional UPDATE to ensure 
        # we don't race with another transaction
        now = datetime.utcnow()
        if session.query(SystemStatusDuration)\
                .filter_by(finish_time=None, id=open_sd.id)\
                .update({'finish_time': now}, synchronize_session=False) \
                != 1:
            raise RuntimeError('System status updated in another transaction')
        # Make the ORM aware of it as well
        open_sd.finish_time = now
        obj.status_durations.insert(0, SystemStatusDuration(status=child))
        return child

class SystemCc(SystemObject):

    def __init__(self, email_address):
        super(SystemCc, self).__init__()
        self.email_address = email_address


class Hypervisor(SystemObject):

    def __repr__(self):
        return self.hypervisor

    @classmethod
    def get_all_types(cls):
        """
        return an array of tuples containing id, hypervisor
        """
        return [(hvisor.id, hvisor.hypervisor) for hvisor in cls.query]

    @classmethod
    def get_all_names(cls):
        return [h.hypervisor for h in cls.query]

    @classmethod
    def by_name(cls, hvisor):
        return cls.query.filter_by(hypervisor=hvisor).one()


class SystemAccessPolicy(DeclBase, MappedObject):

    """
    A list of rules controlling who is allowed to do what to a system.
    """
    __tablename__ = 'system_access_policy'
    __table_args__ = {'mysql_engine': 'InnoDB'}
    id = Column(Integer, nullable=False, primary_key=True)
    system_id = Column(Integer, ForeignKey('system.id',
            name='system_access_policy_system_id_fk'))
    system = relationship(System,
            backref=backref('custom_access_policy', uselist=False))

    @hybrid_method
    def grants(self, user, permission):
        """
        Does this policy grant the given permission to the given user?
        """
        return any(rule.permission == permission and
                (rule.user == user or rule.group in user.groups or rule.everybody)
                for rule in self.rules)

    @grants.expression
    def grants(cls, user, permission):
        # need to avoid passing an empty list to in_
        clauses = [SystemAccessPolicyRule.user == user, SystemAccessPolicyRule.everybody]
        if user.groups:
            clauses.append(SystemAccessPolicyRule.group_id.in_(
                    [g.group_id for g in user.groups]))
        return cls.rules.any(and_(SystemAccessPolicyRule.permission == permission,
                or_(*clauses)))

    @hybrid_method
    def grants_everybody(self, permission):
        """
        Does this policy grant the given permission to all users?
        """
        return any(rule.permission == permission and rule.everybody
                for rule in self.rules)

    @grants_everybody.expression
    def grants_everybody(cls, permission):
        return cls.rules.any(and_(SystemAccessPolicyRule.permission == permission,
                SystemAccessPolicyRule.everybody))

    def add_rule(self, permission, user=None, group=None, everybody=False):
        """
        Pass either user, or group, or everybody=True.
        """
        if user is not None and group is not None:
            raise RuntimeError('Rules are for a user or a group, not both')
        if user is None and group is None and not everybody:
            raise RuntimeError('Did you mean to pass everybody=True to add_rule?')
        session.flush() # make sure self is persisted, for lazy_create
        self.rules.append(SystemAccessPolicyRule.lazy_create(policy_id=self.id,
                permission=permission,
                user_id=user.user_id if user else None,
                group_id=group.group_id if group else None))
        return self.rules[-1]

class SystemAccessPolicyRule(DeclBase, MappedObject):

    """
    A single rule in a system access policy. Policies can have one or more of these.

    The existence of a row in this table means that the given permission is 
    granted to the given user or group in this policy.
    """
    __tablename__ = 'system_access_policy_rule'
    __table_args__ = {'mysql_engine': 'InnoDB'}

    # It would be nice to have a constraint like:
    #    UniqueConstraint('policy_id', 'user_id', 'group_id', 'permission')
    # but we can't because user_id and group_id are NULLable and MySQL has
    # non-standard behaviour for that which makes the constraint useless :-(

    id = Column(Integer, nullable=False, primary_key=True)
    policy_id = Column(Integer, ForeignKey('system_access_policy.id',
            name='system_access_policy_rule_policy_id_fk'), nullable=False)
    policy = relationship(SystemAccessPolicy, backref=backref('rules',
            cascade='all, delete, delete-orphan'))
    # Either user or group is set, to indicate who the rule applies to.
    # If both are NULL, the rule applies to everyone.
    user_id = Column(Integer, ForeignKey('tg_user.user_id',
            name='system_access_policy_rule_user_id_fk'))
    user = relationship(User)
    group_id = Column(Integer, ForeignKey('tg_group.group_id',
            name='system_access_policy_rule_group_id_fk'))
    group = relationship(Group)
    permission = Column(SystemPermission.db_type())

    def __repr__(self):
        return '<grant %s to %s>' % (self.permission,
                self.group or self.user or 'everybody')

    @hybrid_property
    def everybody(self):
        return (self.user == None) & (self.group == None)


class Provision(SystemObject):
    pass


class ProvisionFamily(SystemObject):
    pass


class ProvisionFamilyUpdate(SystemObject):
    pass


class ExcludeOSMajor(SystemObject):
    pass


class ExcludeOSVersion(SystemObject):
    pass


class Watchdog(MappedObject):
    """ Every running task has a corresponding watchdog which will
        Return the system if it runs too long
    """

    @classmethod
    def by_system(cls, system):
        """ Find a watchdog based on the system name
        """
        return cls.query.filter_by(system=system).one()

    @classmethod
    def by_status(cls, labcontroller=None, status="active"):
        """
        Returns a list of all watchdog entries that are either active
        or expired for this lab controller.

        A recipe is only returned as "expired" if all the recipes in the recipe 
        set have expired. Similarly, a recipe is returned as "active" so long 
        as any recipe in the recipe set is still active. Some tasks rely on 
        this behaviour. In particular, the host recipe in virt testing will 
        finish while its guests are still running, but we want to keep 
        monitoring the host's console log in case of a panic.
        """
        select_recipe_set_id = session.query(RecipeSet.id). \
            join(Recipe).join(Watchdog).group_by(RecipeSet.id)
        if status == 'active':
            watchdog_clause = func.max(Watchdog.kill_time) > datetime.utcnow()
        elif status =='expired':
            watchdog_clause = func.max(Watchdog.kill_time) < datetime.utcnow()
        else:
            return None

        recipe_set_in_watchdog = RecipeSet.id.in_(
            select_recipe_set_id.having(watchdog_clause))

        if labcontroller is None:
            my_filter = and_(Watchdog.kill_time != None, recipe_set_in_watchdog)
        else:
            my_filter = and_(RecipeSet.lab_controller==labcontroller,
                Watchdog.kill_time != None, recipe_set_in_watchdog)
        return cls.query.join(Watchdog.recipe, Recipe.recipeset).filter(my_filter)


class LabInfo(SystemObject):
    fields = ['orig_cost', 'curr_cost', 'dimensions', 'weight', 'wattage', 'cooling']


class Cpu(SystemObject):
    def __init__(self, vendor=None, model=None, model_name=None, family=None, stepping=None,speed=None,processors=None,cores=None,sockets=None,flags=None):
        # Intentionally not chaining to super(), to avoid session.add(self)
        self.vendor = vendor
        self.model = model
        self.model_name = model_name
        self.family = family
        self.stepping = stepping
        self.speed = speed
        self.processors = processors
        self.cores = cores
        self.sockets = sockets
        if self.processors > self.cores:
            self.hyper = True
        else:
            self.hyper = False
        self.updateFlags(flags)

    def updateFlags(self,flags):
        if flags != None:
            for cpuflag in flags:
                new_flag = CpuFlag(flag=cpuflag)
                self.flags.append(new_flag)

class CpuFlag(SystemObject):
    def __init__(self, flag=None):
        super(CpuFlag, self).__init__()
        self.flag = flag

    def __repr__(self):
        return self.flag

    def by_flag(cls, flag):
        return cls.query.filter_by(flag=flag)

    by_flag = classmethod(by_flag)


class Numa(SystemObject):
    def __init__(self, nodes=None):
        super(Numa, self).__init__()
        self.nodes = nodes

    def __repr__(self):
        return str(self.nodes)


class DeviceClass(SystemObject):

    @classmethod
    def lazy_create(cls, device_class=None, **kwargs):
        """
        Like the normal lazy_create, but with special handling for
        device_class None -> "NONE".
        """
        if not device_class:
            device_class = 'NONE'
        return super(DeviceClass, cls).lazy_create(
                device_class=device_class, **kwargs)

    def __init__(self, device_class=None, description=None):
        super(DeviceClass, self).__init__()
        if not device_class:
            device_class = "NONE"
        self.device_class = device_class
        self.description = description

    def __repr__(self):
        return self.device_class


class Device(SystemObject):
    pass

class Disk(SystemObject):
    def __init__(self, size=None, sector_size=None, phys_sector_size=None, model=None):
        self.size = int(size)
        self.sector_size = int(sector_size)
        self.phys_sector_size = int(phys_sector_size)
        self.model = model

class PowerType(MappedObject):

    def __init__(self, name=None):
        super(PowerType, self).__init__()
        self.name = name

    @classmethod
    def get_all(cls):
        """
        Apc, wti, etc..
        """
        all_types = cls.query
        return [(0, "None")] + [(type.id, type.name) for type in all_types]

    @classmethod
    def by_name(cls, name):
        return cls.query.filter_by(name=name).one()

    @classmethod
    def by_id(cls, id):
        return cls.query.filter_by(id=id).one()

    @classmethod
    def list_by_name(cls,name,find_anywhere=False):
        if find_anywhere:
            q = cls.query.filter(PowerType.name.like('%%%s%%' % name))
        else:
            q = cls.query.filter(PowerType.name.like('%s%%' % name))
        return q

class Power(SystemObject):
    pass


class SystemActivity(Activity):
    def object_name(self):
        return "System: %s" % self.object.fqdn

class RecipeSetActivity(Activity):
    def object_name(self):
        return "RecipeSet: %s" % self.object.id

class CommandActivity(Activity):
    def __init__(self, user, service, action, status, callback=None):
        Activity.__init__(self, user, service, action, u'Command', u'', u'')
        self.status = status
        self.callback = callback

    def object_name(self):
        return "Command: %s %s" % (self.object.fqdn, self.action)

    def change_status(self, new_status):
        current_status = self.status
        if session.connection(CommandActivity).execute(command_queue_table.update(
                and_(command_queue_table.c.id == self.id,
                     command_queue_table.c.status == current_status)),
                status=new_status).rowcount != 1:
            raise StaleCommandStatusException(
                    'Status for command %s updated in another transaction'
                    % self.id)
        self.status = new_status

    def log_to_system_history(self):
        sa = SystemActivity(self.user, self.service, self.action, u'Power', u'',
                            self.new_value and u'%s: %s' % (self.status, self.new_value) \
                            or u'%s' % self.status)
        self.system.activity.append(sa)

    def abort(self, msg=None):
        log.error('Command %s aborted: %s', self.id, msg)
        self.status = CommandStatus.aborted
        self.new_value = msg
        self.log_to_system_history()

# note model
class Note(MappedObject):
    def __init__(self, user=None, text=None):
        super(Note, self).__init__()
        self.user = user
        self.text = text

    @classmethod
    def all(cls):
        return cls.query

    @property
    def html(self):
        """
        The note's text rendered to HTML using Markdown.
        """
        # Try rendering as markdown, if that fails for any reason, just
        # return the raw text string. The template will take care of the
        # difference (this really doesn't belong in the model, though...)
        try:
            rendered = markdown(self.text, safe_mode='escape')
        except Exception:
            return self.text
        return XML(rendered)


class Key(SystemObject):

    # Obsoleted keys are ones which have been replaced by real, structured 
    # columns on the system table (and its related tables). We disallow users 
    # from searching on these keys in the web UI, to encourage them to migrate 
    # to the structured columns instead (and to avoid the costly queries that 
    # sometimes result).
    obsoleted_keys = [u'MODULE', u'PCIID']

    @classmethod
    def get_all_keys(cls):
        """
        This method's name is deceptive, it actually excludes "obsoleted" keys.
        """
        all_keys = cls.query
        return [key.key_name for key in all_keys
                if key.key_name not in cls.obsoleted_keys]

    @classmethod
    def by_name(cls, key_name):
        return cls.query.filter_by(key_name=key_name).one()


    @classmethod
    def list_by_name(cls, name, find_anywhere=False):
        """
        A class method that can be used to search keys
        based on the key_name
        """
        if find_anywhere:
            q = cls.query.filter(Key.key_name.like('%%%s%%' % name))
        else:
            q = cls.query.filter(Key.key_name.like('%s%%' % name))
        return q

    @classmethod
    def by_id(cls, id):
        return cls.query.filter_by(id=id).one()

    def __init__(self, key_name=None, numeric=False):
        super(Key, self).__init__()
        self.key_name = key_name
        self.numeric = numeric

    def __repr__(self):
        return "%s" % self.key_name


# key_value model
class Key_Value_String(MappedObject):

    key_type = 'string'

    def __init__(self, key, key_value, system=None):
        super(Key_Value_String, self).__init__()
        self.system = system
        self.key = key
        self.key_value = key_value

    def __repr__(self):
        return "%s %s" % (self.key, self.key_value)

    @classmethod
    def by_key_value(cls, system, key, value):
        return cls.query.filter(and_(Key_Value_String.key==key,
                                  Key_Value_String.key_value==value,
                                  Key_Value_String.system==system)).one()


class Key_Value_Int(MappedObject):

    key_type = 'int'

    def __init__(self, key, key_value, system=None):
        super(Key_Value_Int, self).__init__()
        self.system = system
        self.key = key
        self.key_value = key_value

    def __repr__(self):
        return "%s %s" % (self.key, self.key_value)

    @classmethod
    def by_key_value(cls, system, key, value):
        return cls.query.filter(and_(Key_Value_Int.key==key,
                                  Key_Value_Int.key_value==value,
                                  Key_Value_Int.system==system)).one()


class Log(MappedObject):

    MAX_ENTRIES_PER_DIRECTORY = 100

    @staticmethod
    def _normalized_path(path):
        """
        We need to normalize the `path` attribute *before* storing it, so that 
        we don't end up with duplicate rows that point to equivalent filesystem 
        paths (bug 865265).
        Also by convention we use '/' rather than empty string to mean "no 
        subdirectory". It's all a bit weird...
        """
        return re.sub(r'/+', '/', path or u'') or u'/'

    @classmethod
    def lazy_create(cls, path=None, **kwargs):
        return super(Log, cls).lazy_create(path=cls._normalized_path(path), **kwargs)

    def __init__(self, path=None, filename=None,
                 server=None, basepath=None, parent=None):
        # Intentionally not chaining to super(), to avoid session.add(self)
        self.parent = parent
        self.path = self._normalized_path(path)
        self.filename = filename
        self.server = server
        self.basepath = basepath

    def __repr__(self):
        return '%s(path=%r, filename=%r, server=%r, basepath=%r)' % (
                self.__class__.__name__, self.path, self.filename,
                self.server, self.basepath)

    def result(self):
        return self.parent.result

    result = property(result)

    def _combined_path(self):
        """Combines path (which is really the "subdir" of sorts) with filename:
                      , log.txt => log.txt
                /     , log.txt => log.txt
                /debug, log.txt => debug/log.txt
                debug , log.txt => debug/log.txt
        """
        return os.path.join((self.path or '').lstrip('/'), self.filename)

    @property
    def full_path(self):
        """ Like .href, but returns an absolute filesystem path if the log is local. """
        if self.server:
            return self.href
        else:
            return os.path.join(self.parent.logspath, self.parent.filepath,
                self._combined_path())

    @property
    def href(self):
        if self.server:
            # self.server points at a directory so it should end in 
            # a trailing slash, but older versions of the code didn't do that
            url = self.server
            if not url.endswith('/'):
                url += '/'
            return '%s%s' % (url, self._combined_path())
        else:
            return os.path.join('/logs', self.parent.filepath, self._combined_path())

    @property
    def link(self):
        """ Return a link to this Log
        """
        return make_link(url=self.href, text=self._combined_path())

    @property
    def dict(self):
        """ Return a dict describing this log
        """
        return dict( server  = self.server,
                    path     = self.path,
                    filename = self.filename,
                    tid      = '%s:%s' % (self.type, self.id),
                    filepath = self.parent.filepath,
                    basepath = self.basepath,
                    url      = urlparse.urljoin(absolute_url('/'), self.href),
                   )

    @classmethod 
    def by_id(cls,id): 
        return cls.query.filter_by(id=id).one()

    def __cmp__(self, other):
        """ Used to compare logs that are already stored. Log(path,filename) in Recipe.logs  == True
        """
        if hasattr(other,'path'):
            path = other.path
        if hasattr(other,'filename'):
            filename = other.filename
        if "%s/%s" % (self.path,self.filename) == "%s/%s" % (path,filename):
            return 0
        else:
            return 1

class LogRecipe(Log):
    type = 'R'

class LogRecipeTask(Log):
    type = 'T'

class LogRecipeTaskResult(Log):
    type = 'E'

class TaskBase(MappedObject):
    t_id_types = dict(T = 'RecipeTask',
                      TR = 'RecipeTaskResult',
                      R = 'Recipe',
                      RS = 'RecipeSet',
                      J = 'Job')

    @property
    def logspath(self):
        return get('basepath.logs', '/var/www/beaker/logs')

    @classmethod
    def get_by_t_id(cls, t_id, *args, **kw):
        """
        Return an TaskBase object by it's shorthand i.e 'J:xx, RS:xx'
        """
        # Keep Client/doc/bkr.rst in sync with this
        task_type,id = t_id.split(":")
        try:
            class_str = cls.t_id_types[task_type]
        except KeyError:
            raise BeakerException(_('You have have specified an invalid task type:%s' % task_type))

        class_ref = globals()[class_str]
        try:
            obj_ref = class_ref.by_id(id)
        except InvalidRequestError, e:
            raise BeakerException(_('%s is not a valid %s id' % (id, class_str)))

        return obj_ref

    def _change_status(self, new_status, **kw):
        """
        _change_status will update the status if needed
        Returns True when status is changed
        """
        current_status = self.status
        if current_status != new_status:
            # Sanity check to make sure the status never goes backwards.
            if isinstance(self, (Recipe, RecipeTask)) and \
                    ((new_status.queued and not current_status.queued) or \
                     (not new_status.finished and current_status.finished)):
                raise ValueError('Invalid state transition for %s: %s -> %s'
                        % (self.t_id, current_status, new_status))
            # Use a conditional UPDATE to make sure we are really working from 
            # the latest database state.
            # The .base_mapper bit here is so we can get from MachineRecipe to 
            # Recipe, which is needed due to the limitations of .update() 
            if session.query(object_mapper(self).base_mapper)\
                    .filter_by(id=self.id, status=current_status)\
                    .update({'status': new_status}, synchronize_session=False) \
                    != 1:
                raise StaleTaskStatusException(
                        'Status for %s updated in another transaction'
                        % self.t_id)
            # update the ORM session state as well
            self.status = new_status
            return True
        else:
            return False

    def is_finished(self):
        """
        Simply state if the task is finished or not
        """
        return self.status.finished

    def is_queued(self):
        """
        State if the task is queued
        """ 
        return self.status.queued

    @hybrid_method
    def is_failed(self):
        """ 
        Return True if the task has failed
        """
        return (self.result in [TaskResult.warn,
                                TaskResult.fail,
                                TaskResult.panic])
    @is_failed.expression
    def is_failed(cls):
        """
        Return SQL expression that is true if the task has failed
        """
        return cls.result.in_([TaskResult.warn,
                               TaskResult.fail,
                               TaskResult.panic])

    # TODO: it would be good to split the bar definition out to a utility
    # module accepting a mapping of div classes to percentages and then
    # unit test it without needing to create dummy recipes
    @property
    def progress_bar(self):
        """Return proportional progress bar as a HTML div

        Returns None if there are no tasks at all
        """
        if not getattr(self, 'ttasks', None):
            return None
        # Get the width for individual items, using 3 decimal places
        # Even on large screens, this should be a fine enough resolution
        # to fill the bar reliably when all tasks are complete without needing
        # to fiddle directly with the width of any of the subelements
        fmt_style = 'width:%.3f%%'
        pstyle = wstyle = fstyle = kstyle = fmt_style % 0
        completed = 0
        if getattr(self, 'ptasks', None):
            completed += self.ptasks
            pstyle = fmt_style % (100.0 * self.ptasks / self.ttasks)
        if getattr(self, 'wtasks', None):
            completed += self.wtasks
            wstyle = fmt_style % (100.0 * self.wtasks / self.ttasks)
        if getattr(self, 'ftasks', None):
            completed += self.ftasks
            fstyle = fmt_style % (100.0 * self.ftasks / self.ttasks)
        if getattr(self, 'ktasks', None):
            completed += self.ktasks
            kstyle = fmt_style % (100.0 * self.ktasks / self.ttasks)
        # Truncate the overall percentage to ensure it nevers hits 100%
        # before we finish (even if only one task remains in a large recipe)
        percentCompleted = "%d%%" % int(100.0 * completed / self.ttasks)
        # Build the HTML
        div = Element('div', {'class': 'progress'})
        div.append(Element('div', {'class': 'bar bar-success', 'style': pstyle}))
        div.append(Element('div', {'class': 'bar bar-warning', 'style': wstyle}))
        div.append(Element('div', {'class': 'bar bar-danger', 'style': fstyle}))
        div.append(Element('div', {'class': 'bar bar-info', 'style': kstyle}))
        container = Element('div')
        container.text = percentCompleted
        container.append(div)
        return container


    def t_id(self):
        for t, class_ in self.t_id_types.iteritems():
            if self.__class__.__name__ == class_:
                return '%s:%s' % (t, self.id)
    t_id = property(t_id)

    def _get_log_dirs(self):
        """
        Returns the directory names of all a task's logs,
        with a trailing slash.

        URLs are also returned with a trailing slash.
        """
        logs_to_return = []
        for log in self.logs:
            full_path = os.path.dirname(log.full_path)
            if not full_path.endswith('/'):
                full_path += '/'
            logs_to_return.append(full_path)
        return logs_to_return


class Job(TaskBase):
    """
    Container to hold like recipe sets.
    """

    def __init__(self, ttasks=0, owner=None, whiteboard=None,
            retention_tag=None, product=None, group=None, submitter=None):
        # Intentionally not chaining to super(), to avoid session.add(self)
        self.ttasks = ttasks
        self.owner = owner
        if submitter is None:
            self.submitter = owner
        else:
            self.submitter = submitter
        self.group = group
        self.whiteboard = whiteboard
        self.retention_tag = retention_tag
        self.product = product
        self.dirty_version = uuid.uuid4()
        self.clean_version = self.dirty_version

    stop_types = ['abort','cancel']
    max_by_whiteboard = 20

    @classmethod
    def mine(cls, owner):
        """
        Returns a query of all jobs which are owned by the given user.
        """
        return cls.query.filter(or_(Job.owner==owner, Job.submitter==owner))

    @classmethod
    def my_groups(cls, owner):
        """
        ... as in, "my groups' jobs". Returns a query of all jobs which were 
        submitted for any of the given user's groups.
        """
        if owner.groups:
            return cls.query.outerjoin(Job.group)\
                    .filter(Group.group_id.in_([g.group_id for g in owner.groups]))
        else:
            return cls.query.filter(literal(False))

    @classmethod
    def get_nacks(self,jobs):
        queri = select([recipe_set_table.c.id], from_obj=job_table.join(recipe_set_table), whereclause=job_table.c.id.in_(jobs),distinct=True)
        results = queri.execute() 
        current_nacks = []
        for r in results:
            rs_id = r[0]
            rs = RecipeSet.by_id(rs_id)
            response = getattr(rs.nacked,'response',None)
            if response == Response.by_response('nak'):
                current_nacks.append(rs_id)
        return current_nacks

    @classmethod
    def update_nacks(cls,job_ids,rs_nacks):
        """
        update_nacks() takes a list of job_ids and updates the job's recipesets with the correct nacks
        """
        queri = select([recipe_set_table.c.id], from_obj=job_table.join(recipe_set_table), whereclause=job_table.c.id.in_(job_ids),distinct=True)
        results = queri.execute()
        current_nacks = []
        if len(rs_nacks) > 0:
            rs_nacks = map(lambda x: int(x), rs_nacks) # they come in as unicode objs
        for res in results:
            rs_id = res[0]
            rs = RecipeSet.by_id(rs_id)
            if rs_id not in rs_nacks and rs.nacked: #looks like we're deleting it then 
                rs.nacked = []
            else: 
                if not rs.nacked and rs_id in rs_nacks: #looks like we're adding it then
                    rs.nacked = [RecipeSetResponse()]
                    current_nacks.append(rs_id)
                elif rs.nacked:
                    current_nacks.append(rs_id)
                    
        return current_nacks 

    @classmethod
    def complete_delta(cls, delta, query):
        delta = timedelta(**delta)
        if not query:
            query = cls.query
        query = query.join(cls.recipesets, RecipeSet.recipes).filter(and_(Recipe.finish_time < datetime.utcnow() - delta,
            cls.status.in_([status for status in TaskStatus if status.finished])))
        return query

    @classmethod
    def _remove_descendants(cls, list_of_logs):
        """Return a list of paths with common descendants removed
        """
        set_of_logs = set(list_of_logs)
        logs_A = copy(set_of_logs)
        logs_to_return = copy(set_of_logs)

        # This is a simple way to remove descendants,
        # as long as our list of logs doesn't get too large
        for log_A in logs_A:
            for log_B in set_of_logs:
                if log_B.startswith(log_A) and log_A != log_B:
                    try:
                        logs_to_return.remove(log_B)
                    except KeyError:
                        pass # Possibly already removed
        return logs_to_return

    @classmethod
    def expired_logs(cls, limit=None):
        """Iterate over log files for expired recipes

        Will not yield recipes that have already been deleted. Does
        yield recipes that are marked to be deleted though.
        """
        job_ids = [job_id for job_id, in cls.marked_for_deletion().values(Job.id)]
        for tag in RetentionTag.get_transient():
            expire_in = tag.expire_in_days
            tag_name = tag.tag
            job_ids.extend(job_id for job_id, in cls.find_jobs(tag=tag_name,
                complete_days=expire_in, include_to_delete=True).values(Job.id))
        job_ids = list(set(job_ids))
        if limit is not None:
            job_ids = job_ids[:limit]
        for job_id in job_ids:
            job = Job.by_id(job_id)
            logs = job.get_log_dirs()
            if logs:
                logs = cls._remove_descendants(logs)
            yield (job, logs)
        return

    @classmethod
    def has_family(cls, family, query=None, **kw):
        if query is None:
            query = cls.query
        query = query.join(cls.recipesets, RecipeSet.recipes, Recipe.distro_tree, DistroTree.distro, Distro.osversion, OSVersion.osmajor).filter(OSMajor.osmajor == family).reset_joinpoint()
        return query

    @classmethod
    def by_tag(cls, tag, query=None):
        if query is None:
            query = cls.query
        if type(tag) is list:
            tag_query = cls.retention_tag_id.in_([RetentionTag.by_tag(unicode(t)).id for t in tag])
        else:
            tag_query = cls.retention_tag==RetentionTag.by_tag(unicode(tag))
        
        return query.filter(tag_query)

    @classmethod
    def by_product(cls, product, query=None):
        if query is None:
            query=cls.query
        if type(product) is list:
            product_query = cls.product.in_(*[Product.by_name(p) for p in product])
        else:
            product_query = cls.product == Product.by_name(product)
        return query.join('product').filter(product_query)

    @classmethod
    def by_owner(cls, owner, query=None):
        if query is None:
            query=cls.query
        if type(owner) is list:
            owner_query = cls.owner.in_(*[User.by_user_name(p) for p in owner])
        else:
            owner_query = cls.owner == User.by_user_name(owner)
        return query.join('owner').filter(owner_query)

    @classmethod
    def sanitise_job_ids(cls, job_ids):
        """
            sanitise_job_ids takes a list of job ids and returns the list
            sans ids that are not 'valid' (i.e deleted jobs)
        """
        invalid_job_ids = [j[0] for j in cls.marked_for_deletion().values(Job.id)]
        valid_job_ids = []
        for job_id in job_ids:
            if job_id not in invalid_job_ids:
                valid_job_ids.append(job_id)
        return valid_job_ids

    @classmethod
    def sanitise_jobs(cls, query):
        """
            This method will remove any jobs from a query that are
            deemed to not be a 'valid' job
        """
        query = query.filter(and_(cls.to_delete==None, cls.deleted==None))
        return query

    @classmethod
    def by_whiteboard(cls, desc, like=False, only_valid=False):
        if type(desc) is list and len(desc) <= 1:
            desc = desc.pop()
        if type(desc) is list:
            if like:
                if len(desc) > 1:
                    raise ValueError('Cannot perform a like operation with multiple values')
                else:
                    query = Job.query.filter(Job.whiteboard.like('%%%s%%' % desc.pop()))
            else:
                query = Job.query.filter(Job.whiteboard.in_(desc))
        else:
            if like:
                query = Job.query.filter(Job.whiteboard.like('%%%s%%' % desc))
            else:
                query = Job.query.filter_by(whiteboard=desc)
        if only_valid:
            query = cls.sanitise_jobs(query)
        return query

    @classmethod
    def provision_system_job(cls, distro_tree_id, **kw):
        """ Create a new reserve job, if system_id is defined schedule it too """
        job = Job(ttasks=0, owner=identity.current.user, retention_tag=RetentionTag.get_default())
        if kw.get('whiteboard'):
            job.whiteboard = kw.get('whiteboard') 
        if not isinstance(distro_tree_id, list):
            distro_tree_id = [distro_tree_id]

        if job.owner.rootpw_expired:
            raise BX(_(u"Your root password has expired, please change or clear it in order to submit jobs."))

        for id in distro_tree_id:
            try:
                distro_tree = DistroTree.by_id(id)
            except InvalidRequestError:
                raise BX(u'Invalid distro tree ID %s' % id)
            recipeSet = RecipeSet(ttasks=2)
            recipe = MachineRecipe(ttasks=2)
            # Inlcude the XML definition so that cloning this job will act as expected.
            recipe.distro_requires = distro_tree.to_xml().toxml()
            recipe.distro_tree = distro_tree
            # Don't report panic's for reserve workflow.
            recipe.panic = 'ignore'
            system_id = kw.get('system_id')
            if system_id:
                try:
                    system = System.by_id(kw.get('system_id'), identity.current.user)
                except InvalidRequestError:
                    raise BX(u'Invalid System ID %s' % system_id)
                # Inlcude the XML definition so that cloning this job will act as expected.
                recipe.host_requires = system.to_xml().toxml()
                recipe.systems.append(system)
            if kw.get('ks_meta'):
                recipe.ks_meta = kw.get('ks_meta')
            if kw.get('koptions'):
                recipe.kernel_options = kw.get('koptions')
            if kw.get('koptions_post'):
                recipe.kernel_options_post = kw.get('koptions_post')
            # Eventually we will want the option to add more tasks.
            # Add Install task
            recipe.tasks.append(RecipeTask(task = Task.by_name(u'/distribution/install')))
            # Add Reserve task
            reserveTask = RecipeTask(task = Task.by_name(u'/distribution/reservesys'))
            if kw.get('reservetime'):
                #FIXME add DateTimePicker to ReserveSystem Form
                reserveTask.params.append(RecipeTaskParam( name = 'RESERVETIME', 
                                                                value = kw.get('reservetime')
                                                            )
                                        )
            recipe.tasks.append(reserveTask)
            recipeSet.recipes.append(recipe)
            job.recipesets.append(recipeSet)
            job.ttasks += recipeSet.ttasks
        session.add(job)
        session.flush()
        return job

    @classmethod
    def marked_for_deletion(cls):
        return cls.query.filter(and_(cls.to_delete!=None, cls.deleted==None))

    @classmethod
    def find_jobs(cls, query=None, tag=None, complete_days=None, family=None,
        product=None, include_deleted=False, include_to_delete=False,
        owner=None, **kw):
        """Return a filtered job query

        Does what it says. Also helps searching for expired jobs
        easier.
        """
        if not query:
            query = cls.query
        if not include_deleted:
            query = query.filter(Job.deleted == None)
        if not include_to_delete:
            query = query.filter(Job.to_delete == None)
        if complete_days:
            #This takes the same kw names as timedelta
            query = cls.complete_delta({'days':int(complete_days)}, query)
        if family:
            try:
                OSMajor.by_name(family)
            except NoResultFound:
                err_msg = _(u'Family is invalid: %s') % family
                log.exception(err_msg)
                raise BX(err_msg)

            query =cls.has_family(family, query)
        if tag:
            if len(tag) == 1:
                tag = tag[0]
            try:
                query = cls.by_tag(tag, query)
            except NoResultFound:
                err_msg = _('Tag is invalid: %s') % tag
                log.exception(err_msg)
                raise BX(err_msg)

        if product:
            if len(product) == 1:
                product = product[0]
            try:
                query = cls.by_product(product,query)
            except NoResultFound:
                err_msg = _('Product is invalid: %s') % product
                log.exception(err_msg)
                raise BX(err_msg)
        if owner:
            try:
                query = cls.by_owner(owner, query)
            except NoResultFound:
                err_msg = _('Owner is invalid: %s') % owner
                log.exception(err_msg)
                raise BX(err_msg)
        return query

    @classmethod
    def cancel_jobs_by_user(cls, user, msg = None):
        jobs = Job.query.filter(and_(Job.owner == user,
                                     Job.status.in_([s for s in TaskStatus if not s.finished])))
        for job in jobs:
            job.cancel(msg=msg)

    @classmethod
    def delete_jobs(cls, jobs=None, query=None):
        jobs_to_delete  = cls._delete_criteria(jobs,query)

        for job in jobs_to_delete:
            job.soft_delete()

        return jobs_to_delete

    @classmethod
    def _delete_criteria(cls, jobs=None, query=None):
        """Returns valid jobs for deletetion


           takes either a list of Job objects or a query object, and returns
           those that are valid for deletion


        """
        if not jobs and not query:
            raise BeakerException('Need to pass either list of jobs or a query to _delete_criteria')
        valid_jobs = []
        if jobs:
            for j in jobs:
                if j.is_finished() and not j.counts_as_deleted():
                    valid_jobs.append(j)
            return valid_jobs
        elif query:
            query = query.filter(cls.status.in_([status for status in TaskStatus if status.finished]))
            query = query.filter(and_(Job.to_delete == None, Job.deleted == None))
            query = query.filter(Job.owner==identity.current.user)
            return query

    def delete(self):
        """Deletes entries relating to a Job and it's children

            currently only removes log entries of a job and child tasks and marks
            the job as deleted.
            It does not delete other mapped relations or the job row itself.
            it does not remove log FS entries


        """
        for rs in self.recipesets:
            rs.delete()
        self.deleted = datetime.utcnow()

    def counts_as_deleted(self):
        return self.deleted or self.to_delete

    def build_ancestors(self, *args, **kw):
        """
        I have no ancestors
        """
        return ()

    def set_response(self, response):
        for rs in self.recipesets:
            rs.set_response(response)

    def requires_product(self):
        return self.retention_tag.requires_product()

    def soft_delete(self, *args, **kw):
        if self.deleted:
            raise BeakerException(u'%s has already been deleted, cannot delete it again' % self.t_id)
        if self.to_delete:
            raise BeakerException(u'%s is already marked to delete' % self.t_id)
        self.to_delete = datetime.utcnow()

    def get_log_dirs(self):
        logs = []
        for rs in self.recipesets:
            rs_logs = rs.get_log_dirs()
            if rs_logs:
                logs.extend(rs_logs)
        return logs

    @property
    def all_logs(self):
        return sum([rs.all_logs for rs in self.recipesets], [])

    def clone_link(self):
        """ return link to clone this job
        """
        return url("/jobs/clone?job_id=%s" % self.id)

    def cancel_link(self):
        """ return link to cancel this job
        """
        return url("/jobs/cancel?id=%s" % self.id)

    def is_owner(self,user):
        if self.owner == user:
            return True
        return False

    def priority_settings(self, prefix, colspan='1'):
        span = Element('span')
        title = Element('td')
        title.attrib['class']='title' 
        title.text = "Set all RecipeSet priorities"        
        content = Element('td')
        content.attrib['colspan'] = colspan
        for p in TaskPriority:
            id = '%s%s' % (prefix, self.id)
            a_href = make_fake_link(p.value, id, p.value)
            content.append(a_href)
        
        span.append(title)
        span.append(content)
        return span

    def retention_settings(self,prefix,colspan='1'):
        span = Element('span')
        title = Element('td')
        title.attrib['class']='title' 
        title.text = "Set all RecipeSet tags"        
        content = Element('td')
        content.attrib['colspan'] = colspan
        tags = RetentionTag.query.all()
        for t in tags:
            id = '%s%s' % (u'retentiontag_job_', self.id)
            a_href = make_fake_link(unicode(t.id), id, t.tag)
            content.append(a_href)
        span.append(title)
        span.append(content)
        return span

    def _create_job_elem(self,clone=False, *args, **kw):
        job = xmldoc.createElement("job")
        if not clone:
            job.setAttribute("id", "%s" % self.id)
            job.setAttribute("owner", "%s" % self.owner.email_address)
            job.setAttribute("result", "%s" % self.result)
            job.setAttribute("status", "%s" % self.status)
        if self.cc:
            notify = xmldoc.createElement('notify')
            for email_address in self.cc:
                notify.appendChild(node('cc', email_address))
            job.appendChild(notify)
        job.setAttribute("retention_tag", "%s" % self.retention_tag.tag)
        if self.group:
            job.setAttribute("group", "%s" % self.group.group_name)
        if self.product:
            job.setAttribute("product", "%s" % self.product.name)
        job.appendChild(node("whiteboard", self.whiteboard or ''))
        return job

    def to_xml(self, clone=False, *args, **kw):
        job = self._create_job_elem(clone)
        for rs in self.recipesets:
            job.appendChild(rs.to_xml(clone))
        return job

    def cancel(self, msg=None):
        """
        Method to cancel all unfinished tasks in this job.
        """
        for recipeset in self.recipesets:
            for recipe in recipeset.recipes:
                for task in recipe.tasks:
                    if not task.is_finished():
                        task._abort_cancel(TaskStatus.cancelled, msg)
        self._mark_dirty()

    def abort(self, msg=None):
        """
        Method to abort all unfinished tasks in this job.
        """
        for recipeset in self.recipesets:
            for recipe in recipeset.recipes:
                for task in recipe.tasks:
                    if not task.is_finished():
                        task._abort_cancel(TaskStatus.aborted, msg)
        self._mark_dirty()

    def task_info(self):
        """
        Method for exporting job status for TaskWatcher
        """
        return dict(
                    id              = "J:%s" % self.id,
                    worker          = None,
                    state_label     = "%s" % self.status,
                    state           = self.status.value,
                    method          = "%s" % self.whiteboard,
                    result          = "%s" % self.result,
                    is_finished     = self.is_finished(),
                    is_failed       = self.is_failed(),
                    #subtask_id_list = ["R:%s" % r.id for r in self.all_recipes]
                   )

    def all_recipes(self):
        """
        Return all recipes
        """
        for recipeset in self.recipesets:
            for recipe in recipeset.recipes:
                yield recipe
    all_recipes = property(all_recipes)

    def update_status(self):
        self._update_status()
        self._mark_clean()

    def _mark_dirty(self):
        self.dirty_version = uuid.uuid4()

    def _mark_clean(self):
        self.clean_version = self.dirty_version

    @property
    def is_dirty(self):
        return (self.dirty_version != self.clean_version)

    def _update_status(self):
        """
        Update number of passes, failures, warns, panics..
        """
        self.ptasks = 0
        self.wtasks = 0
        self.ftasks = 0
        self.ktasks = 0
        max_result = TaskResult.min()
        min_status = TaskStatus.max()
        for recipeset in self.recipesets:
            recipeset._update_status()
            self.ptasks += recipeset.ptasks
            self.wtasks += recipeset.wtasks
            self.ftasks += recipeset.ftasks
            self.ktasks += recipeset.ktasks
            if recipeset.status.severity < min_status.severity:
                min_status = recipeset.status
            if recipeset.result.severity > max_result.severity:
                max_result = recipeset.result
        status_changed = self._change_status(min_status)
        self.result = max_result
        if status_changed and self.is_finished():
            # Send email notification
            mail.job_notify(self)

    #def t_id(self):
    #    return "J:%s" % self.id
    #t_id = property(t_id)

    @property
    def link(self):
        return make_link(url='/jobs/%s' % self.id, text=self.t_id)

    def can_stop(self, user=None):
        """Return True iff the given user can stop the job"""
        can_stop = self._can_administer(user)
        if not can_stop and user:
            can_stop = user.has_permission('stop_task')
        return can_stop

    def can_change_priority(self, user=None):
        """Return True iff the given user can change the priority"""
        can_change = self._can_administer(user) or self._can_administer_old(user)
        if not can_change and user:
            can_change = user.in_group(['admin','queue_admin'])
        return can_change

    def can_change_whiteboard(self, user=None):
        """Returns True iff the given user can change the whiteboard"""
        return self._can_administer(user) or self._can_administer_old(user)

    def can_change_product(self, user=None):
        """Returns True iff the given user can change the product"""
        return self._can_administer(user) or self._can_administer_old(user)

    def can_change_retention_tag(self, user=None):
        """Returns True iff the given user can change the retention tag"""
        return self._can_administer(user) or self._can_administer_old(user)

    def can_delete(self, user=None):
        """Returns True iff the given user can delete the job"""
        return self._can_administer(user) or self._can_administer_old(user)

    def can_cancel(self, user=None):
        """Returns True iff the given user can cancel the job"""
        return self._can_administer(user)

    def can_set_response(self, user=None):
        """Returns True iff the given user can set the response to this job"""
        return self._can_administer(user) or self._can_administer_old(user)

    def _can_administer(self, user=None):
        """Returns True iff the given user can administer the Job.

        Admins, group job members, job owners, and submitters
        can administer a job.
        """
        if user is None:
            return False
        if self.group:
            if self.group in user.groups:
                return True
        return self.is_owner(user) or user.is_admin() or \
            self.submitter == user

    def _can_administer_old(self, user):
        """
        This fills the gap between the new permissions system with group
        jobs and the old permission model without it.

        XXX Using a config option to enable this deprecated function.
        This code will be removed. Eventually. See BZ#1000861
        """
        if not get('beaker.deprecated_job_group_permissions.on', True):
            return False
        if not user:
            return False
        return bool(set(user.groups).intersection(set(self.owner.groups)))

    cc = association_proxy('_job_ccs', 'email_address')

class JobCc(MappedObject):

    def __init__(self, email_address):
        super(JobCc, self).__init__()
        self.email_address = email_address


class Product(MappedObject):

    def __init__(self, name):
        super(Product, self).__init__()
        self.name = name

    @classmethod
    def by_id(cls, id):
        return cls.query.filter(cls.id == id).one()

    @classmethod
    def by_name(cls, name):
        return cls.query.filter(cls.name == name).one()

class BeakerTag(MappedObject):


    def __init__(self, tag, *args, **kw):
        super(BeakerTag, self).__init__()
        self.tag = tag

    def can_delete(self):
        raise NotImplementedError("Please implement 'can_delete'  on %s" % self.__class__.__name__)

    @classmethod
    def by_id(cls, id, *args, **kw):
        return cls.query.filter(cls.id==id).one()

    @classmethod
    def by_tag(cls, tag, *args, **kw):
        return cls.query.filter(cls.tag==tag).one()

    @classmethod
    def get_all(cls, *args, **kw):
        return cls.query


class RetentionTag(BeakerTag):

    def __init__(self, tag, is_default=False, needs_product=False, expire_in_days=None, *args, **kw):
        self.needs_product = needs_product
        self.expire_in_days = expire_in_days
        self.set_default_val(is_default)
        self.needs_product = needs_product
        super(RetentionTag, self).__init__(tag, **kw)

    @classmethod
    def by_name(cls,tag):
        return cls.query.filter_by(tag=tag).one()

    def can_delete(self):
        if self.is_default:
            return False
        # At the moment only jobs use this tag, update this if that ever changes
        # Only remove tags that haven't been used
        return not bool(Job.query.filter(Job.retention_tag == self).count())

    def requires_product(self):
        return self.needs_product

    def get_default_val(self):
        return self.is_default
    
    def set_default_val(self, is_default):
        if is_default:
            try:
                current_default = self.get_default()
                current_default.is_default = False
            except InvalidRequestError, e: pass
        self.is_default = is_default
    default = property(get_default_val,set_default_val)

    @classmethod
    def get_default(cls, *args, **kw):
        return cls.query.filter(cls.is_default==True).one()

    @classmethod
    def list_by_requires_product(cls, requires=True, *args, **kw):
        return cls.query.filter(cls.needs_product == requires).all()

    @classmethod
    def list_by_tag(cls, tag, anywhere=True, *args, **kw):
        if anywhere is True:
            q = cls.query.filter(cls.tag.like('%%%s%%' % tag))
        else:
            q = cls.query.filter(cls.tag.like('%s%%' % tag))
        return q

    @classmethod
    def get_transient(cls):
        return cls.query.filter(cls.expire_in_days != 0).all()

    def __repr__(self, *args, **kw):
        return self.tag

class Response(MappedObject):

    @classmethod
    def get_all(cls,*args,**kw):
        return cls.query

    @classmethod
    def by_response(cls,response,*args,**kw):
        return cls.query.filter_by(response = response).one()

    def __repr__(self):
        return self.response

    def __str__(self):
        return self.response

class RecipeSetResponse(MappedObject):
    """
    An acknowledgment of a RecipeSet's results. Can be used for filtering reports
    """

    def __init__(self,type=None,response_id=None,comment=None):
        super(RecipeSetResponse, self).__init__()
        if response_id is not None:
            res = Response.by_id(response_id)
        elif type is not None:
            res = Response.by_response(type)
        self.response = res
        self.comment = comment

    @classmethod 
    def by_id(cls,id): 
        return cls.query.filter_by(recipe_set_id=id).one()

    @classmethod
    def by_jobs(cls,job_ids):
        if isinstance(job_ids, list):
            clause = Job.id.in_(job_ids)
        elif isinstance(job_ids, int):
            clause = Job.id == job_ids
        else:
            raise BeakerException('job_ids needs to be either type \'int\' or \'list\'. Found %s' % type(job_ids))
        queri = cls.query.outerjoin('recipesets','job').filter(clause)
        results = {}
        for elem in queri:
            results[elem.recipe_set_id] = elem.comment
        return results

class RecipeSet(TaskBase):
    """
    A Collection of Recipes that must be executed at the same time.
    """
    stop_types = ['abort','cancel']

    def __init__(self, ttasks=0, priority=None):
        # Intentionally not chaining to super(), to avoid session.add(self)
        self.ttasks = ttasks
        self.priority = priority

    def get_log_dirs(self):
        logs = []
        for recipe in self.recipes:
            r_logs = recipe.get_log_dirs()
            if r_logs:
                logs.extend(r_logs)
        return logs

    @property
    def all_logs(self):
        return sum([recipe.all_logs for recipe in self.recipes], [])

    def set_response(self, response):
        if self.nacked is None:
            self.nacked = RecipeSetResponse(type=response)
        else:
            self.nacked.response = Response.by_response(response)

    def is_owner(self,user):
        if self.owner == user:
            return True
        return False

    def can_set_response(self, user=None):
        """Return True iff the given user can change the response to this recipeset"""
        return self.job.can_set_response(user)

    def can_stop(self, user=None):
        """Returns True iff the given user can stop this recipeset"""
        return self.job.can_stop(user)

    def can_cancel(self, user=None):
        """Returns True iff the given user can cancel this recipeset"""
        return self.job.can_cancel(user)

    def build_ancestors(self, *args, **kw):
        """
        return a tuple of strings containing the Recipes RS and J
        """
        return (self.job.t_id,)

    def owner(self):
        return self.job.owner
    owner = property(owner)

    def to_xml(self, clone=False, from_job=True, *args, **kw):
        recipeSet = xmldoc.createElement("recipeSet")
        recipeSet.setAttribute('priority', unicode(self.priority))
        return_node = recipeSet 

        if not clone:
            response = self.get_response()
            if response:
                recipeSet.setAttribute('response','%s' % str(response))

        if not clone:
            recipeSet.setAttribute("id", "%s" % self.id)

        for r in self.machine_recipes:
            recipeSet.appendChild(r.to_xml(clone, from_recipeset=True))
        if not from_job:
            job = self.job._create_job_elem(clone)
            job.appendChild(recipeSet)
            return_node = job
        return return_node

    @property
    def machine_recipes(self):
        for recipe in self.recipes:
            if not isinstance(recipe, GuestRecipe):
                yield recipe

    def delete(self):
        for r in self.recipes:
            r.delete()

    @classmethod
    def allowed_priorities_initial(cls,user):
        if not user:
            return
        if user.in_group(['admin','queue_admin']):
            return [pri for pri in TaskPriority]
        default = TaskPriority.default_priority()
        return [pri for pri in TaskPriority
                if TaskPriority.index(pri) < TaskPriority.index(default)]

    @classmethod
    def by_tag(cls, tag, query=None):
        if query is None:
            query = cls.query
        if type(tag) is list:
            tag_query = cls.retention_tag_id.in_([RetentionTag.by_tag(unicode(t)).id for t in tag])
        else:
            tag_query = cls.retention_tag==RetentionTag.by_tag(unicode(tag))
        
        return query.filter(tag_query)

    @classmethod
    def by_datestamp(cls, datestamp, query=None):
        if not query:
            query=cls.query
        return query.filter(RecipeSet.queue_time <= datestamp)

    @classmethod 
    def by_id(cls,id): 
        return cls.query.filter_by(id=id).one()

    @classmethod
    def by_job_id(cls,job_id):
        queri = RecipeSet.query.outerjoin('job').filter(Job.id == job_id)
        return queri

    def cancel(self, msg=None):
        """
        Method to cancel all unfinished tasks in this recipe set.
        """
        for recipe in self.recipes:
            for task in recipe.tasks:
                if not task.is_finished():
                    task._abort_cancel(TaskStatus.cancelled, msg)
        self.job._mark_dirty()

    def abort(self, msg=None):
        """
        Method to abort all unfinished tasks in this recipe set.
        """
        for recipe in self.recipes:
            for task in recipe.tasks:
                if not task.is_finished():
                    task._abort_cancel(TaskStatus.aborted, msg)
        self.job._mark_dirty()

    @property
    def is_dirty(self):
        return self.job.is_dirty

    def _update_status(self):
        """
        Update number of passes, failures, warns, panics..
        """
        self.ptasks = 0
        self.wtasks = 0
        self.ftasks = 0
        self.ktasks = 0
        max_result = TaskResult.min()
        min_status = TaskStatus.max()
        for recipe in self.recipes:
            recipe._update_status()
            self.ptasks += recipe.ptasks
            self.wtasks += recipe.wtasks
            self.ftasks += recipe.ftasks
            self.ktasks += recipe.ktasks
            if recipe.status.severity < min_status.severity:
                min_status = recipe.status
            if recipe.result.severity > max_result.severity:
                max_result = recipe.result
        self._change_status(min_status)
        self.result = max_result

        # Return systems if recipeSet finished
        if self.is_finished():
            for recipe in self.recipes:
                recipe.cleanup()

    def machine_recipes_orderby(self, labcontroller):
        query = select([recipe_table.c.id, 
                        func.count(System.id).label('count')],
                        from_obj=[recipe_table, 
                                  system_recipe_map,
                                  system_table,
                                  recipe_set_table,
                                  lab_controller_table],
                        whereclause="recipe.id = system_recipe_map.recipe_id \
                             AND  system.id = system_recipe_map.system_id \
                             AND  system.lab_controller_id = lab_controller.id \
                             AND  recipe_set.id = recipe.recipe_set_id \
                             AND  recipe_set.id = %s \
                             AND  lab_controller.id = %s" % (self.id, 
                                                            labcontroller.id),
                        group_by=[Recipe.id],
                        order_by='count')
        return map(lambda x: MachineRecipe.query.filter_by(id=x[0]).first(), session.connection(RecipeSet).execute(query).fetchall())

    def get_response(self):
        response = getattr(self.nacked,'response',None)
        return response

    def task_info(self):
        """
        Method for exporting RecipeSet status for TaskWatcher
        """
        return dict(
                    id              = "RS:%s" % self.id,
                    worker          = None,
                    state_label     = "%s" % self.status,
                    state           = self.status.value,
                    method          = None,
                    result          = "%s" % self.result,
                    is_finished     = self.is_finished(),
                    is_failed       = self.is_failed(),
                    #subtask_id_list = ["R:%s" % r.id for r in self.recipes]
                   )
 
    def allowed_priorities(self,user):
        if not user:
            return [] 
        if user.in_group(['admin','queue_admin']):
            return [pri for pri in TaskPriority]
        elif user == self.job.owner: 
            return [pri for pri in TaskPriority
                    if TaskPriority.index(pri) <= TaskPriority.index(self.priority)]

    def cancel_link(self):
        """ return link to cancel this recipe
        """
        return url("/recipesets/cancel?id=%s" % self.id)

    def clone_link(self):
        """ return link to clone this recipe
        """
        return url("/jobs/clone?recipeset_id=%s" % self.id)


class Recipe(TaskBase):
    """
    Contains requires for host selection and distro selection.
    Also contains what tasks will be executed.
    """
    stop_types = ['abort','cancel']

    def __init__(self, ttasks=0):
        # Intentionally not chaining to super(), to avoid session.add(self)
        self.ttasks = ttasks

    def crypt_root_password(self):
        if self.recipeset.job.group:
            group_pw = self.recipeset.job.group.root_password
            if group_pw:
                if len(group_pw.split('$')) != 4:
                    salt = ''.join(random.choice(string.digits + string.ascii_letters)
                                   for i in range(8))
                    return crypt.crypt(group_pw, "$1$%s$" % salt)
                else:
                    return group_pw
        # if it is not a group job or the group password is not set
        return self.owner.root_password

    @property
    def harnesspath(self):
        return get('basepath.harness', '/var/www/beaker/harness')

    @property
    def repopath(self):
        return get('basepath.repos', '/var/www/beaker/repos')

    def is_owner(self,user):
        return self.recipeset.job.owner == user

    def is_deleted(self):
        if self.recipeset.job.deleted or self.recipeset.job.to_delete:
            return True
        return False

    def build_ancestors(self, *args, **kw):
        """
        return a tuple of strings containing the Recipes RS and J
        """
        return (self.recipeset.job.t_id, self.recipeset.t_id)

    def clone_link(self):
        """ return link to clone this recipe
        """
        return url("/jobs/clone?recipeset_id=%s" % self.recipeset.id)

    @property
    def link(self):
        """ Return a link to this recipe. """
        return make_link(url='/recipes/%s' % self.id, text=self.t_id,
                elem_class='recipe-id')

    def filepath(self):
        """
        Return file path for this recipe
        """
        job    = self.recipeset.job
        return "%s/%02d/%s/%s/%s" % (self.recipeset.queue_time.year,
                self.recipeset.queue_time.month,
                job.id // Log.MAX_ENTRIES_PER_DIRECTORY, job.id, self.id)
    filepath = property(filepath)

    def get_log_dirs(self):
        recipe_logs = self._get_log_dirs()
        for task in self.tasks:
            rt_log = task.get_log_dirs()
            if rt_log:
                recipe_logs.extend(rt_log)
        return recipe_logs

    def owner(self):
        return self.recipeset.job.owner
    owner = property(owner)

    def delete(self):
        """
        How we delete a Recipe.
        """
        self.logs = []
        if self.rendered_kickstart:
            session.delete(self.rendered_kickstart)
            self.rendered_kickstart = None
        for task in self.tasks:
            task.delete()

    def task_repo(self):
        return ('beaker-tasks',absolute_url('/repos/%s' % self.id,
                                            scheme='http',
                                            labdomain=True,
                                            webpath=False,
                                           )
               )

    def harness_repo(self):
        """
        return repos needed for harness and task install
        """
        if self.distro_tree:
            if os.path.exists("%s/%s" % (self.harnesspath,
                                            self.distro_tree.distro.osversion.osmajor)):
                return ('beaker-harness',
                    absolute_url('/harness/%s/' %
                                 self.distro_tree.distro.osversion.osmajor,
                                 scheme='http',
                                 labdomain=True,
                                 webpath=False,
                                )
                       )

    def generated_install_options(self):
        ks_meta = {
            'packages': ':'.join(p.package for p in self.packages),
            'customrepos': [dict(repo_id=r.name, path=r.url) for r in self.repos],
            'harnessrepo': '%s,%s' % self.harness_repo(),
            'taskrepo': '%s,%s' % self.task_repo(),
            'partitions': self.partitionsKSMeta,
        }
        return InstallOptions(ks_meta, {}, {})

    def to_xml(self, recipe, clone=False, from_recipeset=False, from_machine=False):
        if not clone:
            recipe.setAttribute("id", "%s" % self.id)
            recipe.setAttribute("job_id", "%s" % self.recipeset.job_id)
            recipe.setAttribute("recipe_set_id", "%s" % self.recipe_set_id)
        autopick = xmldoc.createElement("autopick")
        autopick.setAttribute("random", "%s" % unicode(self.autopick_random).lower())
        recipe.appendChild(autopick)
        recipe.setAttribute("whiteboard", "%s" % self.whiteboard and self.whiteboard or '')
        recipe.setAttribute("role", "%s" % self.role and self.role or 'RECIPE_MEMBERS')
        if self.kickstart:
            kickstart = xmldoc.createElement("kickstart")
            text = xmldoc.createCDATASection('%s' % self.kickstart)
            kickstart.appendChild(text)
            recipe.appendChild(kickstart)
        if self.rendered_kickstart and not clone:
            recipe.setAttribute('kickstart_url', self.rendered_kickstart.link)
        recipe.setAttribute("ks_meta", "%s" % self.ks_meta and self.ks_meta or '')
        recipe.setAttribute("kernel_options", "%s" % self.kernel_options and self.kernel_options or '')
        recipe.setAttribute("kernel_options_post", "%s" % self.kernel_options_post and self.kernel_options_post or '')
        if self.duration and not clone:
            recipe.setAttribute("duration", "%s" % self.duration)
        if self.result and not clone:
            recipe.setAttribute("result", "%s" % self.result)
        if self.status and not clone:
            recipe.setAttribute("status", "%s" % self.status)
        if self.distro_tree and not clone:
            recipe.setAttribute("distro", "%s" % self.distro_tree.distro.name)
            recipe.setAttribute("arch", "%s" % self.distro_tree.arch)
            recipe.setAttribute("family", "%s" % self.distro_tree.distro.osversion.osmajor)
            recipe.setAttribute("variant", "%s" % self.distro_tree.variant)
        watchdog = xmldoc.createElement("watchdog")
        if self.panic:
            watchdog.setAttribute("panic", "%s" % self.panic)
        recipe.appendChild(watchdog)
        if self.resource and self.resource.fqdn and not clone:
            recipe.setAttribute("system", "%s" % self.resource.fqdn)
        packages = xmldoc.createElement("packages")
        if self.custom_packages:
            for package in self.custom_packages:
                packages.appendChild(package.to_xml())
        recipe.appendChild(packages)

        ks_appends = xmldoc.createElement("ks_appends")
        if self.ks_appends:
            for ks_append in self.ks_appends:
                ks_appends.appendChild(ks_append.to_xml())
        recipe.appendChild(ks_appends)
            
        if not self.is_queued() and not clone:
            roles = xmldoc.createElement("roles")
            for role in self.roles_to_xml():
                roles.appendChild(role)
            recipe.appendChild(roles)
        repos = xmldoc.createElement("repos")
        for repo in self.repos:
            repos.appendChild(repo.to_xml())
        recipe.appendChild(repos)
        drs = xml.dom.minidom.parseString(self.distro_requires)
        hrs = xml.dom.minidom.parseString(self.host_requires)
        for dr in drs.getElementsByTagName("distroRequires"):
            recipe.appendChild(dr)
        hostRequires = xmldoc.createElement("hostRequires")
        for hr in hrs.getElementsByTagName("hostRequires"):
            for child in hr.childNodes[:]:
                hostRequires.appendChild(child)
        recipe.appendChild(hostRequires)
        prs = xml.dom.minidom.parseString(self.partitions)
        partitions = xmldoc.createElement("partitions")
        for pr in prs.getElementsByTagName("partitions"):
            for child in pr.childNodes[:]:
                partitions.appendChild(child)
        recipe.appendChild(partitions)
        for t in self.tasks:
            recipe.appendChild(t.to_xml(clone))
        if not from_recipeset and not from_machine:
            recipe = self._add_to_job_element(recipe, clone)
        return recipe

    def _add_to_job_element(self, recipe, clone):
        recipeSet = xmldoc.createElement("recipeSet")
        recipeSet.appendChild(recipe)
        job = xmldoc.createElement("job")
        if not clone:
            job.setAttribute("owner", "%s" % self.recipeset.job.owner.email_address)
        job.appendChild(node("whiteboard", self.recipeset.job.whiteboard or ''))
        job.appendChild(recipeSet)
        return job

    def _get_duration(self):
        try:
            return self.finish_time - self.start_time
        except TypeError:
            return None
    duration = property(_get_duration)

    def _get_packages(self):
        """ return all packages for all tests
        """
        packages = []
        packages.extend(TaskPackage.query
                .select_from(RecipeTask).join(Task).join(Task.required)
                .filter(RecipeTask.recipe == self)
                .order_by(TaskPackage.package).distinct())
        packages.extend(self.custom_packages)
        return packages

    packages = property(_get_packages)

    def _get_arch(self):
        if self.distro_tree:
            return self.distro_tree.arch

    arch = property(_get_arch)

    def _get_host_requires(self):
        # If no system_type is specified then add defaults
        try:
            hrs = xml.dom.minidom.parseString(self._host_requires)
        except TypeError:
            hrs = xmldoc.createElement("hostRequires")
        except xml.parsers.expat.ExpatError:
            hrs = xmldoc.createElement("hostRequires")
        if not hrs.getElementsByTagName("system_type"):
            hostRequires = xmldoc.createElement("hostRequires")
            for hr in hrs.getElementsByTagName("hostRequires"):
                for child in hr.childNodes[:]:
                    hostRequires.appendChild(child)
            system_type = xmldoc.createElement("system_type")
            system_type.setAttribute("value", "%s" % self.systemtype)
            hostRequires.appendChild(system_type)
            return hostRequires.toxml()
        else:
            return hrs.toxml()

    def _set_host_requires(self, value):
        self._host_requires = value

    host_requires = property(_get_host_requires, _set_host_requires)

    def _get_partitions(self):
        """ get _partitions """
        try:
            prs = xml.dom.minidom.parseString(self._partitions)
        except TypeError:
            prs = xmldoc.createElement("partitions")
        except xml.parsers.expat.ExpatError:
            prs = xmldoc.createElement("partitions")
        return prs.toxml()

    def _set_partitions(self, value):
        """ set _partitions """
        self._partitions = value

    partitions = property(_get_partitions, _set_partitions)

    def _partitionsKSMeta(self):
        """ Parse partitions xml into ks_meta variable which cobbler will understand """
        partitions = []
        try:
            prs = xml.dom.minidom.parseString(self.partitions)
        except TypeError:
            prs = xmldoc.createElement("partitions")
        except xml.parsers.expat.ExpatError:
            prs = xmldoc.createElement("partitions")
        for partition in prs.getElementsByTagName("partition"):
            fs = partition.getAttribute('fs')
            name = partition.getAttribute('name')
            type = partition.getAttribute('type') or 'part'
            size = partition.getAttribute('size') or '5'
            if fs:
                partitions.append('%s:%s:%s:%s' % (name, type, size, fs))
            else:
                partitions.append('%s:%s:%s' % (name, type, size))
        return ';'.join(partitions)
    partitionsKSMeta = property(_partitionsKSMeta)

    def queue(self):
        """
        Move from Processed -> Queued
        """
        for task in self.tasks:
            task._change_status(TaskStatus.queued)
        self.recipeset.job._mark_dirty()
        # purely as an optimisation
        self._change_status(TaskStatus.queued)

    def process(self):
        """
        Move from New -> Processed
        """
        for task in self.tasks:
            task._change_status(TaskStatus.processed)
        self.recipeset.job._mark_dirty()
        # purely as an optimisation
        self._change_status(TaskStatus.processed)

    def createRepo(self):
        """
        Create Recipe specific task repo based on the tasks requested.
        """
        snapshot_repo = os.path.join(self.repopath, str(self.id))
        # The repo may already exist if beakerd.virt_recipes() creates a
        # repo but the subsequent virt provisioning fails and the recipe
        # falls back to being queued on a regular system
        makedirs_ignore(snapshot_repo, 0755)
        Task.make_snapshot_repo(snapshot_repo)
        return True

    def destroyRepo(self):
        """
        Done with Repo, destroy it.
        """
        directory = '%s/%s' % (self.repopath, self.id)
        if os.path.isdir(directory):
            try:
                shutil.rmtree(directory)
            except OSError:
                if os.path.isdir(directory):
                    #something else must have gone wrong
                    raise

    def schedule(self):
        """
        Move from Queued -> Scheduled
        """
        for task in self.tasks:
            task._change_status(TaskStatus.scheduled)
        self.recipeset.job._mark_dirty()
        # purely as an optimisation
        self._change_status(TaskStatus.scheduled)

    def waiting(self):
        """
        Move from Scheduled to Waiting
        """
        for task in self.tasks:
            task._change_status(TaskStatus.waiting)
        self.recipeset.job._mark_dirty()
        # purely as an optimisation
        self._change_status(TaskStatus.waiting)

    def cancel(self, msg=None):
        """
        Method to cancel all unfinished tasks in this recipe.
        """
        for task in self.tasks:
            if not task.is_finished():
                task._abort_cancel(TaskStatus.cancelled, msg)
        self.recipeset.job._mark_dirty()

    def abort(self, msg=None):
        """
        Method to abort all unfinished tasks in this recipe.
        """
        for task in self.tasks:
            if not task.is_finished():
                task._abort_cancel(TaskStatus.aborted, msg)
        self.recipeset.job._mark_dirty()

    @property
    def is_dirty(self):
        return self.recipeset.job.is_dirty

    def _update_status(self):
        """
        Update number of passes, failures, warns, panics..
        """
        self.ptasks = 0
        self.wtasks = 0
        self.ftasks = 0
        self.ktasks = 0

        max_result = TaskResult.min()
        min_status = TaskStatus.max()
        # I think this loop could be replaced with some sql which would be more efficient.
        for task in self.tasks:
            task._update_status()
            if task.is_finished():
                if task.result == TaskResult.pass_:
                    self.ptasks += 1
                if task.result == TaskResult.warn:
                    self.wtasks += 1
                if task.result == TaskResult.fail:
                    self.ftasks += 1
                if task.result == TaskResult.panic:
                    self.ktasks += 1
            if task.status.severity < min_status.severity:
                min_status = task.status
            if task.result.severity > max_result.severity:
                max_result = task.result
        if self.status.finished and not min_status.finished:
            min_status = self._fix_zombie_tasks()
        status_changed = self._change_status(min_status)
        self.result = max_result

        # Record the start of this Recipe.
        if not self.start_time \
           and self.status == TaskStatus.running:
            self.start_time = datetime.utcnow()

        if self.start_time and not self.finish_time and self.is_finished():
            # Record the completion of this Recipe.
            self.finish_time = datetime.utcnow()

        if status_changed and self.is_finished():
            metrics.increment('counters.recipes_%s' % self.status.name)
            if self.status == TaskStatus.aborted and \
                    getattr(self.resource, 'system', None) and \
                    get('beaker.reliable_distro_tag', None) in self.distro_tree.distro.tags:
                self.resource.system.suspicious_abort()

        if self.is_finished():
            # If we have any guests which haven't started, kill them now 
            # because there is no way they can ever start.
            for guest in getattr(self, 'guests', []):
                if (not guest.is_finished() and
                        guest.watchdog and not guest.watchdog.kill_time):
                    guest.abort(msg='Aborted: host %s finished but guest never started'
                            % self.t_id)

    def _fix_zombie_tasks(self):
        # It's not possible to get into this state in recent version of Beaker, 
        # but very old recipes may be finished while still having tasks that 
        # are running. We don't want to restart the recipe though, so we need 
        # to kill the zombie tasks.
        log.debug('Fixing zombie tasks in %s', self.t_id)
        assert self.is_finished()
        assert not self.watchdog
        for task in self.tasks:
            if task.status.severity < self.status.severity:
                task._change_status(self.status)
        return self.status

    def provision(self):
        if not self.harness_repo():
            raise ValueError('Failed to find repo for harness')
        from bkr.server.kickstart import generate_kickstart
        install_options = self.resource.install_options(self.distro_tree)\
                .combined_with(self.generated_install_options())\
                .combined_with(InstallOptions.from_strings(self.ks_meta,
                    self.kernel_options, self.kernel_options_post))
        if 'ks' in install_options.kernel_options:
            # Use it as is
            pass
        elif self.kickstart:
            # add in cobbler packages snippet...
            packages_slot = 0
            nopackages = True
            for line in self.kickstart.split('\n'):
                # Add the length of line + newline
                packages_slot += len(line) + 1
                if line.find('%packages') == 0:
                    nopackages = False
                    break
            beforepackages = self.kickstart[:packages_slot-1]
            afterpackages = self.kickstart[packages_slot:]
            # if no %packages section then add it
            if nopackages:
                beforepackages = "%s\n%%packages --ignoremissing" % beforepackages
                afterpackages = "{{ end }}\n%s" % afterpackages
            # Fill in basic requirements for RHTS
            if self.distro_tree.distro.osversion.osmajor.osmajor == u'RedHatEnterpriseLinux3':
                kicktemplate = """
%(beforepackages)s
{%% snippet 'rhts_packages' %%}
%(afterpackages)s

%%pre
(
{%% snippet 'rhts_pre' %%}
) 2>&1 | /usr/bin/tee /dev/console

%%post
(
{%% snippet 'rhts_post' %%}
) 2>&1 | /usr/bin/tee /dev/console
                """
            else:
                kicktemplate = """
%(beforepackages)s
{%% snippet 'rhts_packages' %%}
%(afterpackages)s

%%pre --log=/dev/console
{%% snippet 'rhts_pre' %%}
{{ end }}

%%post --log=/dev/console
{%% snippet 'rhts_post' %%}
{{ end }}
                """
            kickstart = kicktemplate % dict(
                                        beforepackages = beforepackages,
                                        afterpackages = afterpackages)
            self.rendered_kickstart = generate_kickstart(install_options,
                    distro_tree=self.distro_tree,
                    system=getattr(self.resource, 'system', None),
                    user=self.recipeset.job.owner,
                    recipe=self, kickstart=kickstart)
            install_options.kernel_options['ks'] = self.rendered_kickstart.link
        else:
            ks_appends = [ks_append.ks_append for ks_append in self.ks_appends]
            self.rendered_kickstart = generate_kickstart(install_options,
                    distro_tree=self.distro_tree,
                    system=getattr(self.resource, 'system', None),
                    user=self.recipeset.job.owner,
                    recipe=self, ks_appends=ks_appends)
            install_options.kernel_options['ks'] = self.rendered_kickstart.link

        if isinstance(self.resource, SystemResource):
            self.resource.system.configure_netboot(self.distro_tree,
                    install_options.kernel_options_str,
                    service=u'Scheduler',
                    callback=u'bkr.server.model.auto_cmd_handler')
            self.resource.system.action_power(action=u'reboot',
                                     callback=u'bkr.server.model.auto_cmd_handler')
            self.resource.system.activity.append(SystemActivity(
                    user=self.recipeset.job.owner,
                    service=u'Scheduler', action=u'Provision',
                    field_name=u'Distro Tree', old_value=u'',
                    new_value=unicode(self.distro_tree)))
        elif isinstance(self.resource, VirtResource):
            with VirtManager() as manager:
                manager.start_install(self.resource.system_name,
                        self.distro_tree, install_options.kernel_options_str,
                        self.resource.lab_controller)
            self.tasks[0].start()

    def cleanup(self):
        # Note that this may be called *many* times for a recipe, even when it 
        # has already been cleaned up, so we have to handle that gracefully 
        # (and cheaply!)
        self.destroyRepo()
        if self.resource:
            self.resource.release()
        if self.watchdog:
            session.delete(self.watchdog)
            self.watchdog = None

    def task_info(self):
        """
        Method for exporting Recipe status for TaskWatcher
        """
        worker = {}
        if self.resource:
            worker['name'] = self.resource.fqdn
        return dict(
                    id              = "R:%s" % self.id,
                    worker          = worker,
                    state_label     = "%s" % self.status,
                    state           = self.status.value,
                    method          = "%s" % self.whiteboard,
                    result          = "%s" % self.result,
                    is_finished     = self.is_finished(),
                    is_failed       = self.is_failed(),
# Disable tasks status, TaskWatcher needs to do this differently.  its very resource intesive to make
# so many xmlrpc calls.
#                    subtask_id_list = ["T:%s" % t.id for t in self.tasks],
                   )

    def extend(self, kill_time):
        """
        Extend the watchdog by kill_time seconds
        """
        if not self.watchdog:
            raise BX(_('No watchdog exists for recipe %s' % self.id))
        self.watchdog.kill_time = datetime.utcnow() + timedelta(
                                                              seconds=kill_time)
        return self.status_watchdog()

    def status_watchdog(self):
        """
        Return the number of seconds left on the current watchdog if it exists.
        """
        if self.watchdog:
            delta = self.watchdog.kill_time - datetime.utcnow()
            return delta.seconds + (86400 * delta.days)
        else:
            return False

    @property
    def all_tasks(self):
        """
        Return all tasks and task-results, along with associated logs
        """
        for task in self.tasks:
            yield task
            for task_result in task.results:
                yield task_result

    @property
    def all_logs(self):
        """
        Return all logs for this recipe
        """
        return [mylog.dict for mylog in self.logs] + \
               sum([task.all_logs for task in self.tasks], [])

    def is_task_applicable(self, task):
        """ Does the given task apply to this recipe?
            ie: not excluded for this distro family or arch.
        """
        if self.distro_tree.arch in [arch.arch for arch in task.excluded_arch]:
            return False
        if self.distro_tree.distro.osversion.osmajor in [osmajor.osmajor for osmajor in task.excluded_osmajor]:
            return False
        return True

    @classmethod
    def mine(cls, owner):
        """
        A class method that can be used to search for Jobs that belong to a user
        """
        return cls.query.filter(Recipe.recipeset.has(
                RecipeSet.job.has(Job.owner == owner)))

    def peer_roles(self):
        """
        Returns dict of (role -> recipes) for all "peer" recipes (recipes in 
        the same recipe set as this recipe, *including this recipe*).
        """
        result = {}
        for peer in self.recipeset.recipes:
            result.setdefault(peer.role, []).append(peer)
        return result

    def roles_to_xml(self):
        for key, recipes in sorted(self.peer_roles().iteritems()):
            role = xmldoc.createElement("role")
            role.setAttribute("value", "%s" % key)
            for recipe in recipes:
                if recipe.resource:
                    system = xmldoc.createElement("system")
                    system.setAttribute("value", "%s" % recipe.resource.fqdn)
                    role.appendChild(system)
            yield(role)

    @property
    def first_task(self):
        return self.dyn_tasks.order_by(RecipeTask.id).first()


class GuestRecipe(Recipe):
    systemtype = 'Virtual'

    def to_xml(self, clone=False, from_recipeset=False, from_machine=False):
        recipe = xmldoc.createElement("guestrecipe")
        recipe.setAttribute("guestname", "%s" % (self.guestname or ""))
        recipe.setAttribute("guestargs", "%s" % self.guestargs)
        if self.resource and self.resource.mac_address and not clone:
            recipe.setAttribute("mac_address", "%s" % self.resource.mac_address)
        if self.distro_tree and self.recipeset.lab_controller and not clone:
            location = self.distro_tree.url_in_lab(self.recipeset.lab_controller)
            if location:
                recipe.setAttribute("location", location)
            for lca in self.distro_tree.lab_controller_assocs:
                if lca.lab_controller == self.recipeset.lab_controller:
                    scheme = urlparse.urlparse(lca.url).scheme
                    attr = '%s_location' % re.sub(r'[^a-z0-9]+', '_', scheme.lower())
                    recipe.setAttribute(attr, lca.url)

        return Recipe.to_xml(self, recipe, clone, from_recipeset, from_machine)

    def _add_to_job_element(self, guestrecipe, clone):
        recipe = xmldoc.createElement('recipe')
        if self.resource and not clone:
            recipe.setAttribute('system', '%s' % self.hostrecipe.resource.fqdn)
        recipe.appendChild(guestrecipe)
        job = super(GuestRecipe, self)._add_to_job_element(recipe, clone)
        return job

    def _get_distro_requires(self):
        try:
            drs = xml.dom.minidom.parseString(self._distro_requires)
        except TypeError:
            drs = xmldoc.createElement("distroRequires")
        except xml.parsers.expat.ExpatError:
            drs = xmldoc.createElement("distroRequires")
        return drs.toxml()

    def _set_distro_requires(self, value):
        self._distro_requires = value

    def t_id(self):
        return 'R:%s' % self.id
    t_id = property(t_id)

    distro_requires = property(_get_distro_requires, _set_distro_requires)

class MachineRecipe(Recipe):
    """
    Optionally can contain guest recipes which are just other recipes
      which will be executed on this system.
    """
    systemtype = 'Machine'
    def to_xml(self, clone=False, from_recipeset=False):
        recipe = xmldoc.createElement("recipe")
        for guest in self.guests:
            recipe.appendChild(guest.to_xml(clone, from_machine=True))
        return Recipe.to_xml(self, recipe, clone, from_recipeset)

    def check_virtualisability(self):
        """
        Decide whether this recipe can be run as a virt guest
        """
        # oVirt is i386/x86_64 only
        if self.distro_tree.arch.arch not in [u'i386', u'x86_64']:
            return RecipeVirtStatus.precluded
        # Can't run VMs in a VM
        if self.guests:
            return RecipeVirtStatus.precluded
        # Multihost testing won't work (for now!)
        if len(self.recipeset.recipes) > 1:
            return RecipeVirtStatus.precluded
        # Check we can translate any host requirements into VM params
        # Delayed import to avoid circular dependency
        from bkr.server.needpropertyxml import vm_params, NotVirtualisable
        try:
            vm_params(self.host_requires)
        except NotVirtualisable:
            return RecipeVirtStatus.precluded
        # Checks all passed, so dynamic virt should be attempted
        return RecipeVirtStatus.possible

    @classmethod
    def get_queue_stats(cls, recipes=None):
        """Returns a dictionary of status:count pairs for active recipes"""
        if recipes is None:
            recipes = cls.query
        active_statuses = [s for s in TaskStatus if not s.finished]
        query = (recipes.group_by(cls.status)
                  .having(cls.status.in_(active_statuses))
                  .values(cls.status, func.count(cls.id)))
        result = dict((status.name, 0) for status in active_statuses)
        result.update((status.name, count) for status, count in query)
        return result

    @classmethod
    def get_queue_stats_by_group(cls, grouping, recipes=None):
        """Returns a mapping from named groups to dictionaries of status:count pairs for active recipes
        """
        if recipes is None:
            recipes = cls.query
        active_statuses = [s for s in TaskStatus if not s.finished]
        query = (recipes.with_entities(grouping,
                                       cls.status,
                                       func.count(cls.id))
                 .group_by(grouping, cls.status)
                 .having(cls.status.in_(active_statuses)))
        def init_group_stats():
            return dict((status.name, 0) for status in active_statuses)
        result = defaultdict(init_group_stats)
        for group, status, count in query:
            result[group][status.name] = count
        return result

    def _get_distro_requires(self):
        return self._distro_requires

    def _set_distro_requires(self, value):
        self._distro_requires = value

    def t_id(self):
        return 'R:%s' % self.id
    t_id = property(t_id)

    distro_requires = property(_get_distro_requires, _set_distro_requires)


class RecipeTag(MappedObject):
    """
    Each recipe can be tagged with information that identifies what is being
    executed.  This is helpful when generating reports.
    """
    pass


class RecipeTask(TaskBase):
    """
    This holds the results/status of the task being executed.
    """
    result_types = ['pass_','warn','fail','panic', 'result_none']
    stop_types = ['stop','abort','cancel']

    def __init__(self, task):
        # Intentionally not chaining to super(), to avoid session.add(self)
        self.task = task

    def delete(self): 
        self.logs = []
        for r in self.results:
            r.delete()

    def filepath(self):
        """
        Return file path for this task
        """
        job    = self.recipe.recipeset.job
        recipe = self.recipe
        return "%s/%02d/%s/%s/%s/%s" % (recipe.recipeset.queue_time.year,
                recipe.recipeset.queue_time.month,
                job.id // Log.MAX_ENTRIES_PER_DIRECTORY, job.id,
                recipe.id, self.id)
    filepath = property(filepath)

    def build_ancestors(self, *args, **kw):
        return (self.recipe.recipeset.job.t_id, self.recipe.recipeset.t_id, self.recipe.t_id)

    def get_log_dirs(self):
        recipe_task_logs = self._get_log_dirs()
        for result in self.results:
            rtr_log = result.get_log_dirs()
            if rtr_log:
                recipe_task_logs.extend(rtr_log)
        return recipe_task_logs

    def to_xml(self, clone=False, *args, **kw):
        task = xmldoc.createElement("task")
        task.setAttribute("name", "%s" % self.task.name)
        task.setAttribute("role", "%s" % self.role and self.role or 'STANDALONE')
        if not clone:
            task.setAttribute("id", "%s" % self.id)
            task.setAttribute("avg_time", "%s" % self.task.avg_time)
            task.setAttribute("result", "%s" % self.result)
            task.setAttribute("status", "%s" % self.status)
            rpm = xmldoc.createElement("rpm")
            name = self.task.rpm[:self.task.rpm.find('-%s' % self.task.version)]
            rpm.setAttribute("name", name)
            rpm.setAttribute("path", "%s" % self.task.path)
            task.appendChild(rpm)
        if self.duration and not clone:
            task.setAttribute("duration", "%s" % self.duration)
        if not self.is_queued() and not clone:
            roles = xmldoc.createElement("roles")
            for role in self.roles_to_xml():
                roles.appendChild(role)
            task.appendChild(roles)
        params = xmldoc.createElement("params")
        for p in self.params:
            params.appendChild(p.to_xml())
        task.appendChild(params)
        if self.results and not clone:
            results = xmldoc.createElement("results")
            for result in self.results:
                results.appendChild(result.to_xml())
            task.appendChild(results)
        return task

    def _get_duration(self):
        duration = None
        if self.finish_time and self.start_time:
            duration =  self.finish_time - self.start_time
        elif self.watchdog and self.watchdog.kill_time:
            duration =  'Time Remaining %.7s' % (self.watchdog.kill_time - datetime.utcnow())
        return duration
    duration = property(_get_duration)

    def path(self):
        return self.task.name
    path = property(path)

    def link_id(self):
        """ Return a link to this Executed Recipe->Task
        """
        return make_link(url = '/recipes/%s#task%s' % (self.recipe.id, self.id),
                         text = 'T:%s' % self.id)

    link_id = property(link_id)

    def link(self):
        """ Return a link to this Task
        """
        return make_link(url = '/tasks/%s' % self.task.id,
                         text = self.task.name)

    link = property(link)

    @property
    def all_logs(self):
        return [mylog.dict for mylog in self.logs] + \
               sum([result.all_logs for result in self.results], [])

    @property
    def is_dirty(self):
        return False

    def _update_status(self):
        """
        Update number of passes, failures, warns, panics..
        """
        # The self.result == TaskResult.new condition is just an optimisation 
        # to avoid constantly recomputing the result after the task is finished
        if self.is_finished() and self.result == TaskResult.new:
            max_result = TaskResult.min()
            for result in self.results:
                if result.result.severity > max_result.severity:
                    max_result = result.result
            self.result = max_result

    def start(self, watchdog_override=None):
        """
        Record the start of this task
         If watchdog_override is defined we will use that time instead
         of what the tasks default time is.  This should be defined in number
         of seconds
        """
        if self.is_finished():
            raise BX(_('Cannot restart finished task'))
        if not self.recipe.watchdog:
            raise BX(_('No watchdog exists for recipe %s' % self.recipe.id))
        if not self.start_time:
            self.start_time = datetime.utcnow()
        self._change_status(TaskStatus.running)
        self.recipe.watchdog.recipetask = self
        if watchdog_override:
            self.recipe.watchdog.kill_time = watchdog_override
        else:
            # add in 30 minutes at a minimum
            self.recipe.watchdog.kill_time = datetime.utcnow() + timedelta(
                                                    seconds=self.task.avg_time + 1800)
        self.recipe.recipeset.job._mark_dirty()
        return True

    def extend(self, kill_time):
        """
        Extend the watchdog by kill_time seconds
        """
        return self.recipe.extend(kill_time)

    def status_watchdog(self):
        """
        Return the number of seconds left on the current watchdog if it exists.
        """
        return self.recipe.status_watchdog()

    def stop(self, *args, **kwargs):
        """
        Record the completion of this task
        """
        if not self.recipe.watchdog:
            raise BX(_('No watchdog exists for recipe %s' % self.recipe.id))
        if not self.start_time:
            raise BX(_('recipe task %s was never started' % self.id))
        if self.start_time and not self.finish_time:
            self.finish_time = datetime.utcnow()
        self._change_status(TaskStatus.completed)
        self.recipe.recipeset.job._mark_dirty()
        return True

    def owner(self):
        return self.recipe.recipeset.job.owner
    owner = property(owner)

    def cancel(self, msg=None):
        """
        Cancel this task
        """
        self._abort_cancel(TaskStatus.cancelled, msg)
        self.recipe.recipeset.job._mark_dirty()

    def abort(self, msg=None):
        """
        Abort this task
        """
        self._abort_cancel(TaskStatus.aborted, msg)
        self.recipe.recipeset.job._mark_dirty()

    def _abort_cancel(self, status, msg=None):
        """
        cancel = User instigated
        abort  = Auto instigated
        """
        if self.start_time:
            self.finish_time = datetime.utcnow()
        self._change_status(status)
        self.results.append(RecipeTaskResult(recipetask=self,
                                   path=u'/',
                                   result=TaskResult.warn,
                                   score=0,
                                   log=msg))

    def pass_(self, path, score, summary):
        """
        Record a pass result 
        """
        return self._result(TaskResult.pass_, path, score, summary)

    def fail(self, path, score, summary):
        """
        Record a fail result 
        """
        return self._result(TaskResult.fail, path, score, summary)

    def warn(self, path, score, summary):
        """
        Record a warn result 
        """
        return self._result(TaskResult.warn, path, score, summary)

    def panic(self, path, score, summary):
        """
        Record a panic result 
        """
        return self._result(TaskResult.panic, path, score, summary)

    def result_none(self, path, score, summary):
        return self._result(TaskResult.none, path, score, summary)

    def _result(self, result, path, score, summary):
        """
        Record a result 
        """
        if self.is_finished():
            raise ValueError('Cannot record result for finished task %s' % self.t_id)
        recipeTaskResult = RecipeTaskResult(recipetask=self,
                                   path=path,
                                   result=result,
                                   score=score,
                                   log=summary)
        self.results.append(recipeTaskResult)
        # Flush the result to the DB so we can return the id.
        session.add(recipeTaskResult)
        session.flush()
        return recipeTaskResult.id

    def task_info(self):
        """
        Method for exporting Task status for TaskWatcher
        """
        worker = {}
        if self.recipe.resource:
            worker['name'] = self.recipe.resource.fqdn
        return dict(
                    id              = "T:%s" % self.id,
                    worker          = worker,
                    state_label     = "%s" % self.status,
                    state           = self.status.value,
                    method          = "%s" % self.task.name,
                    result          = "%s" % self.result,
                    is_finished     = self.is_finished(),
                    is_failed       = self.is_failed(),
                    #subtask_id_list = ["TR:%s" % tr.id for tr in self.results]
                   )

    def no_value(self):
        return None
   
    score = property(no_value)

    def peer_roles(self):
        """
        Returns dict of (role -> recipetasks) for all "peer" RecipeTasks, 
        *including this RecipeTask*. A peer RecipeTask is one which appears at 
        the same position in another recipe from the same recipe set as this 
        recipe.
        """
        result = {}
        i = self.recipe.tasks.index(self)
        for peer in self.recipe.recipeset.recipes:
            # Roles are only shared amongst like recipe types
            if type(self.recipe) != type(peer):
                continue
            if i >= len(peer.tasks):
                # We have uneven tasks
                continue
            peertask = peer.tasks[i]
            result.setdefault(peertask.role, []).append(peertask)
        return result

    def roles_to_xml(self):
        for key, recipetasks in sorted(self.peer_roles().iteritems()):
            role = xmldoc.createElement("role")
            role.setAttribute("value", "%s" % key)
            for recipetask in recipetasks:
                if recipetask.recipe.resource:
                    system = xmldoc.createElement("system")
                    system.setAttribute("value", "%s" % recipetask.recipe.resource.fqdn)
                    role.appendChild(system)
            yield(role)

    def can_stop(self, user=None):
        """Returns True iff the given user can stop this recipe task"""
        return self.recipe.recipeset.job.can_stop(user)


class RecipeTaskParam(MappedObject):
    """
    Parameters for task execution.
    """

    def __init__(self, name, value):
        # Intentionally not chaining to super(), to avoid session.add(self)
        self.name = name
        self.value = value

    def to_xml(self):
        param = xmldoc.createElement("param")
        param.setAttribute("name", "%s" % self.name)
        param.setAttribute("value", "%s" % self.value)
        return param


class RecipeRepo(MappedObject):
    """
    Custom repos 
    """

    def __init__(self, name, url):
        # Intentionally not chaining to super(), to avoid session.add(self)
        self.name = name
        self.url = url

    def to_xml(self):
        repo = xmldoc.createElement("repo")
        repo.setAttribute("name", "%s" % self.name)
        repo.setAttribute("url", "%s" % self.url)
        return repo


class RecipeKSAppend(MappedObject):
    """
    Kickstart appends
    """

    def __init__(self, ks_append):
        # Intentionally not chaining to super(), to avoid session.add(self)
        self.ks_append = ks_append

    def to_xml(self):
        ks_append = xmldoc.createElement("ks_append")
        text = xmldoc.createCDATASection('%s' % self.ks_append)
        ks_append.appendChild(text)
        return ks_append

    def __repr__(self):
        return self.ks_append

class RecipeTaskComment(MappedObject):
    """
    User comments about the task execution.
    """
    pass


class RecipeTaskBugzilla(MappedObject):
    """
    Any bugzillas filed/found due to this task execution.
    """
    pass


class RecipeRpm(MappedObject):
    """
    A list of rpms that were installed at the time.
    """
    pass


class RecipeTaskRpm(MappedObject):
    """
    the versions of the RPMS listed in the tasks runfor list.
    """
    pass


class RecipeTaskResult(TaskBase):
    """
    Each task can report multiple results
    """

    def __init__(self, recipetask=None, path=None, result=None,
            score=None, log=None):
        # Intentionally not chaining to super(), to avoid session.add(self)
        self.recipetask = recipetask
        self.path = path
        self.result = result
        self.score = score
        self.log = log

    def filepath(self):
        """
        Return file path for this result
        """
        job    = self.recipetask.recipe.recipeset.job
        recipe = self.recipetask.recipe
        task_id   = self.recipetask.id
        return "%s/%02d/%s/%s/%s/%s/%s" % (recipe.recipeset.queue_time.year,
                recipe.recipeset.queue_time.month,
                job.id // Log.MAX_ENTRIES_PER_DIRECTORY, job.id,
                recipe.id, task_id, self.id)
    filepath = property(filepath)

    def delete(self, *args, **kw):
        self.logs = []

    def to_xml(self, *args, **kw):
        """
        Return result in xml
        """
        result = xmldoc.createElement("result")
        result.setAttribute("id", "%s" % self.id)
        result.setAttribute("path", "%s" % self.path)
        result.setAttribute("result", "%s" % self.result)
        result.setAttribute("score", "%s" % self.score)
        result.appendChild(xmldoc.createTextNode("%s" % self.log))
        #FIXME Append any binary logs as URI's
        return result

    @property
    def all_logs(self):
        return [mylog.dict for mylog in self.logs]

    def get_log_dirs(self):
        return self._get_log_dirs()

    def task_info(self):
        """
        Method for exporting RecipeTaskResult status for TaskWatcher
        """
        return dict(
                    id              = "TR:%s" % self.id,
                    worker          = dict(name = "%s" % None),
                    state_label     = "%s" % self.result,
                    state           = self.result.value,
                    method          = "%s" % self.path,
                    result          = "%s" % self.result,
                    is_finished     = True,
                    is_failed       = False
                   )

    def t_id(self):
        return "TR:%s" % self.id
    t_id = property(t_id)

    @property
    def short_path(self):
        """
        Remove the parent from the begining of the path if present
        """
        if not self.path or self.path == '/':
            short_path = self.log or './'
        elif self.path.rstrip('/') == self.recipetask.task.name:
            short_path = './'
        elif self.path.startswith(self.recipetask.task.name + '/'):
            short_path = self.path.replace(self.recipetask.task.name + '/', '', 1)
        else:
            short_path = self.path
        return short_path

class RecipeResource(MappedObject):
    """
    Base class for things on which a recipe can be run.
    """

    def __str__(self):
        return unicode(self).encode('utf8')

    def __unicode__(self):
        return unicode(self.fqdn)

    @staticmethod
    def _lowest_free_mac():
        base_addr = netaddr.EUI(get('beaker.base_mac_addr', '52:54:00:00:00:00'))
        session.flush()
        # These subqueries gives all MAC addresses in use right now
        guest_mac_query = session.query(GuestResource.mac_address.label('mac_address'))\
                .filter(GuestResource.mac_address != None)\
                .join(RecipeResource.recipe).join(Recipe.recipeset)\
                .filter(not_(RecipeSet.status.in_([s for s in TaskStatus if s.finished])))
        virt_mac_query = session.query(VirtResource.mac_address.label('mac_address'))\
                .filter(VirtResource.mac_address != None)\
                .join(RecipeResource.recipe).join(Recipe.recipeset)\
                .filter(not_(RecipeSet.status.in_([s for s in TaskStatus if s.finished])))
        # This trickery finds "gaps" of unused MAC addresses by filtering for MAC
        # addresses where address + 1 is not in use.
        # We union with base address - 1 to find any gap at the start.
        # Note that this relies on the MACAddress type being represented as
        # BIGINT in the database, which lets us do arithmetic on it.
        left_side = union(guest_mac_query, virt_mac_query,
                select([int(base_addr) - 1])).alias('left_side')
        right_side = union(guest_mac_query, virt_mac_query).alias('right_side')
        free_addr = session.scalar(select([left_side.c.mac_address + 1],
                from_obj=left_side.outerjoin(right_side,
                    onclause=left_side.c.mac_address + 1 == right_side.c.mac_address))\
                .where(right_side.c.mac_address == None)\
                .where(left_side.c.mac_address + 1 >= int(base_addr))\
                .order_by(left_side.c.mac_address).limit(1))
        # The type of (left_side.c.mac_address + 1) comes out as Integer
        # instead of MACAddress, I think it's a sqlalchemy bug :-(
        return netaddr.EUI(free_addr, dialect=mac_unix_padded_dialect)

class SystemResource(RecipeResource):
    """
    For a recipe which is running on a Beaker system.
    """

    def __init__(self, system):
        super(SystemResource, self).__init__()
        self.system = system
        self.fqdn = system.fqdn

    def __repr__(self):
        return '%s(fqdn=%r, system=%r, reservation=%r)' % (
                self.__class__.__name__, self.fqdn, self.system,
                self.reservation)

    @property
    def mac_address(self):
        # XXX the type of system.mac_address should be changed to MACAddress,
        # but for now it's not
        return netaddr.EUI(self.system.mac_address, dialect=mac_unix_padded_dialect)

    @property
    def link(self):
        return make_link(url='/view/%s' % self.system.fqdn,
                         text=self.fqdn)

    def install_options(self, distro_tree):
        return self.system.install_options(distro_tree)

    def allocate(self):
        log.debug('Reserving system %s for recipe %s', self.system, self.recipe.id)
        self.reservation = self.system.reserve_for_recipe(
                                         service=u'Scheduler',
                                         user=self.recipe.recipeset.job.owner)

    def release(self):
        # system_resource rows for very old recipes may have no reservation
        if not self.reservation or self.reservation.finish_time:
            return
        log.debug('Releasing system %s for recipe %s',
            self.system, self.recipe.id)
        self.system.unreserve(service=u'Scheduler',
            reservation=self.reservation,
            user=self.recipe.recipeset.job.owner)


class VirtResource(RecipeResource):
    """
    For a MachineRecipe which is running on a virtual guest managed by 
    a hypervisor attached to Beaker.
    """

    def __init__(self, system_name):
        super(VirtResource, self).__init__()
        self.system_name = system_name

    @property
    def link(self):
        return self.fqdn # just text, not a link

    def install_options(self, distro_tree):
        # 'postreboot' is added as a hack for RHEV guests: they do not reboot
        # properly when the installation finishes, see RHBZ#751854
        return global_install_options()\
                .combined_with(distro_tree.install_options())\
                .combined_with(InstallOptions({'postreboot': None}, {}, {}))

    def allocate(self, manager, lab_controllers):
        self.mac_address = self._lowest_free_mac()
        log.debug('Creating vm with MAC address %s for recipe %s',
                self.mac_address, self.recipe.id)

        virtio_possible = True
        if self.recipe.distro_tree.distro.osversion.osmajor.osmajor == "RedHatEnterpriseLinux3":
            virtio_possible = False

        self.lab_controller = manager.create_vm(self.system_name,
                lab_controllers, self.mac_address, virtio_possible)

    def release(self):
        try:
            log.debug('Releasing vm %s for recipe %s',
                    self.system_name, self.recipe.id)
            with VirtManager() as manager:
                manager.destroy_vm(self.system_name)
        except Exception:
            log.exception('Failed to destroy vm %s, leaked!',
                    self.system_name)
            # suppress exception, nothing more we can do now


class GuestResource(RecipeResource):
    """
    For a GuestRecipe which is running on a guest associated with a parent 
    MachineRecipe.
    """

    def __repr__(self):
        return '%s(fqdn=%r, mac_address=%r)' % (self.__class__.__name__,
                self.fqdn, self.mac_address)

    @property
    def link(self):
        return self.fqdn # just text, not a link

    def install_options(self, distro_tree):
        return global_install_options().combined_with(
                distro_tree.install_options())

    def allocate(self):
        self.mac_address = self._lowest_free_mac()
        log.debug('Allocated MAC address %s for recipe %s', self.mac_address, self.recipe.id)

    def release(self):
        pass

class RenderedKickstart(MappedObject):

    def __repr__(self):
        return '%s(id=%r, kickstart=%s, url=%r)' % (self.__class__.__name__,
                self.id, '<%s chars>' % len(self.kickstart)
                if self.kickstart is not None else 'None', self.url)

    @property
    def link(self):
        if self.url:
            return self.url
        assert self.id is not None, 'not flushed?'
        url = absolute_url('/kickstart/%s' % self.id, scheme='http',
                           labdomain=True)
        return url

# Helper for manipulating the task library

class TaskLibrary(object):

    @property
    def rpmspath(self):
        # Lazy lookup so module can be imported prior to configuration
        return get("basepath.rpms", "/var/www/beaker/rpms")

    def get_rpm_path(self, rpm_name):
        return os.path.join(self.rpmspath, rpm_name)

    def _unlink_locked_rpm(self, rpm_name):
        # Internal call that assumes the flock is already held
        unlink_ignore(self.get_rpm_path(rpm_name))

    def unlink_rpm(self, rpm_name):
        """
        Ensures an RPM is no longer present in the task library
        """
        with Flock(self.rpmspath):
            self._unlink_locked_rpm(rpm_name)

    def _update_locked_repo(self):
        # Internal call that assumes the flock is already held
        # Removed --baseurl, if upgrading you will need to manually
        # delete repodata directory before this will work correctly.
        command, returncode, out, err = run_createrepo(cwd=self.rpmspath)
        if out:
            log.debug("stdout from %s: %s", command, out)
        if err:
            log.warn("stderr from %s: %s", command, err)
        if returncode != 0:
            raise RuntimeError('Createrepo failed.\nreturncode:%s cmd:%s err:%s'
                % (returncode, command, err))

    def update_repo(self):
        """Update the task library yum repo metadata"""
        with Flock(self.rpmspath):
            self._update_locked_repo()

    def _all_rpms(self):
        """Iterator over the task RPMs currently on disk"""
        basepath = self.rpmspath
        for name in os.listdir(basepath):
            if not name.endswith("rpm"):
                continue
            srcpath = os.path.join(basepath, name)
            if os.path.isdir(srcpath):
                continue
            yield srcpath, name

    def _link_rpms(self, dst):
        """Hardlink the task rpms into dst"""
        makedirs_ignore(dst, 0755)
        for srcpath, name in self._all_rpms():
            dstpath = os.path.join(dst, name)
            unlink_ignore(dstpath)
            os.link(srcpath, dstpath)

    def make_snapshot_repo(self, repo_dir):
        """Create a snapshot of the current state of the task library"""
        # This should only run if we are missing repodata in the rpms path
        # since this should normally be updated when new tasks are uploaded
        src_meta = os.path.join(self.rpmspath, 'repodata')
        if not os.path.isdir(src_meta):
            log.info("Task library repodata missing, generating...")
            self.update_repo()
        dst_meta = os.path.join(repo_dir, 'repodata')
        if os.path.isdir(dst_meta):
            log.info("Destination repodata already exists, skipping snapshot")
        else:
            # Copy updated repo to recipe specific repo
            log.debug("Generating task library snapshot")
            with Flock(self.rpmspath):
                self._link_rpms(repo_dir)
                shutil.copytree(src_meta, dst_meta)

    def update_task(self, rpm_name, write_rpm):
        """Updates the specified task

           write_rpm must be a callable that takes a file object as its
           sole argument and populates it with the raw task RPM contents

           Expects to be called in a transaction, and for that transaction
           to be rolled back if an exception is thrown.
        """
        # XXX (ncoghlan): How do we get rid of that assumption about the
        # transaction handling? Assuming we're *not* already in a transaction
        # won't work either.
        rpm_path = self.get_rpm_path(rpm_name)
        upgrade = AtomicFileReplacement(rpm_path)
        f = upgrade.create_temp()
        try:
            write_rpm(f)
            f.flush()
            f.seek(0)
            task = Task.create_from_taskinfo(self.read_taskinfo(f))
            old_rpm_name = task.rpm
            task.rpm = rpm_name
            with Flock(self.rpmspath):
                upgrade.replace_dest()
                try:
                    self._update_locked_repo()
                except:
                    # We assume the current transaction is going to be rolled back,
                    # so the Task possibly defined above, or changes to an existing
                    # task, will never by written to the database (even if it was
                    # the _update_locked_repo() call that failed).
                    # Accordingly, we also throw away the newly created RPM.
                    self._unlink_locked_rpm(rpm_name)
                    raise
                # New task has been added, throw away the old one
                if old_rpm_name:
                    self._unlink_locked_rpm(old_rpm_name)
                    # Since it existed when we called _update_locked_repo()
                    # above, this RPM will still be referenced from the
                    # metadata, albeit not as the latest version.
                    # However, it's too expensive (several seconds of IO
                    # with the task repo locked) to do it twice for every
                    # task update, so we rely on the fact that tasks are
                    # referenced by name rather than requesting specific
                    # versions, and thus will always grab the latest.
        finally:
            # This is a no-op if we successfully replaced the destination
            upgrade.destroy_temp()
        return task

    def get_rpm_info(self, fd):
        """Returns rpm information by querying a rpm"""
        ts = rpm.ts()
        fd.seek(0)
        try:
            hdr = ts.hdrFromFdno(fd.fileno())
        except rpm.error:
            ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)
            fd.seek(0)
            hdr = ts.hdrFromFdno(fd.fileno())
        return { 'name': hdr[rpm.RPMTAG_NAME], 
                    'ver' : "%s-%s" % (hdr[rpm.RPMTAG_VERSION],
                                    hdr[rpm.RPMTAG_RELEASE]), 
                    'epoch': hdr[rpm.RPMTAG_EPOCH],
                    'arch': hdr[rpm.RPMTAG_ARCH] , 
                    'files': hdr['filenames']}

    def read_taskinfo(self, fd):
        """Retrieve Beaker task details from an RPM"""
        taskinfo = dict(desc = '',
                        hdr  = '',
                        )
        taskinfo['hdr'] = self.get_rpm_info(fd)
        taskinfo_file = None
        for file in taskinfo['hdr']['files']:
            if file.endswith('testinfo.desc'):
                taskinfo_file = file
        if taskinfo_file:
            fd.seek(0)
            p1 = subprocess.Popen(["rpm2cpio"],
                                  stdin=fd.fileno(), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
            p2 = subprocess.Popen(["cpio", "--quiet", "--extract",
                                   "--to-stdout", ".%s" % taskinfo_file],
                                  stdin=p1.stdout, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
            taskinfo['desc'] = p2.communicate()[0]
        return taskinfo


class Task(MappedObject):
    """
    Tasks that are available to schedule
    """

    library = TaskLibrary()

    @classmethod
    def by_name(cls, name, valid=None):
        query = cls.query.filter(Task.name==name)
        if valid is not None:
            query = query.filter(Task.valid==bool(valid))
        return query.one()

    @classmethod
    def by_id(cls, id, valid=None):
        query = cls.query.filter(Task.id==id)
        if valid is not None:
            query = query.filter(Task.valid==bool(valid))
        return query.one()

    @classmethod
    def by_type(cls, type, query=None):
        if not query:
            query=cls.query
        return query.join('types').filter(TaskType.type==type)

    @classmethod
    def by_package(cls, package, query=None):
        if not query:
            query=cls.query
        return query.join('runfor').filter(TaskPackage.package==package)

    @classmethod
    def get_rpm_path(cls, rpm_name):
        return cls.library.get_rpm_path(rpm_name)

    @classmethod
    def update_task(cls, rpm_name, write_rpm):
        return cls.library.update_task(rpm_name, write_rpm)

    @classmethod
    def make_snapshot_repo(cls, repo_dir):
        return cls.library.make_snapshot_repo(repo_dir)

    @classmethod
    def create_from_taskinfo(cls, raw_taskinfo):
        """Create a new task object based on details retrieved from an RPM"""

        tinfo = testinfo.parse_string(raw_taskinfo['desc'])

        if len(tinfo.test_name) > 255:
            raise BX(_("Task name should be <= 255 characters"))
        if tinfo.test_name.endswith('/'):
            raise BX(_(u'Task name must not end with slash'))
        if '//' in tinfo.test_name:
            raise BX(_(u'Task name must not contain redundant slashes'))

        task = cls.lazy_create(name=tinfo.test_name)

        # RPM is the same version we have. don't process
        if task.version == raw_taskinfo['hdr']['ver']:
            raise BX(_("Failed to import,  %s is the same version we already have" % task.version))

        task.version = raw_taskinfo['hdr']['ver']
        task.description = tinfo.test_description
        task.types = []
        task.bugzillas = []
        task.required = []
        task.runfor = []
        task.needs = []
        task.excluded_osmajor = []
        task.excluded_arch = []
        includeFamily=[]
        for family in tinfo.releases:
            if family.startswith('-'):
                try:
                    if family.lstrip('-') not in task.excluded_osmajor:
                        task.excluded_osmajor.append(TaskExcludeOSMajor(osmajor=OSMajor.by_name_alias(family.lstrip('-'))))
                except InvalidRequestError:
                    pass
            else:
                try:
                    includeFamily.append(OSMajor.by_name_alias(family).osmajor)
                except InvalidRequestError:
                    pass
        families = set([ '%s' % family.osmajor for family in OSMajor.query])
        if includeFamily:
            for family in families.difference(set(includeFamily)):
                if family not in task.excluded_osmajor:
                    task.excluded_osmajor.append(TaskExcludeOSMajor(osmajor=OSMajor.by_name_alias(family)))
        if tinfo.test_archs:
            arches = set([ '%s' % arch.arch for arch in Arch.query])
            for arch in arches.difference(set(tinfo.test_archs)):
                if arch not in task.excluded_arch:
                    task.excluded_arch.append(TaskExcludeArch(arch=Arch.by_name(arch)))
        task.avg_time = tinfo.avg_test_time
        for type in tinfo.types:
            ttype = TaskType.lazy_create(type=type)
            task.types.append(ttype)
        for bug in tinfo.bugs:
            task.bugzillas.append(TaskBugzilla(bugzilla_id=bug))
        task.path = tinfo.test_path
        # Bug 772882. Remove duplicate required package here
        # Avoid ORM insert in task_packages_required_map twice.
        tinfo.runfor = list(set(tinfo.runfor))
        for runfor in tinfo.runfor:
            package = TaskPackage.lazy_create(package=runfor)
            task.runfor.append(package)
        task.priority = tinfo.priority
        task.destructive = tinfo.destructive
        # Bug 772882. Remove duplicate required package here
        # Avoid ORM insert in task_packages_required_map twice.
        tinfo.requires = list(set(tinfo.requires))
        for require in tinfo.requires:
            package = TaskPackage.lazy_create(package=require)
            task.required.append(package)
        for need in tinfo.needs:
            task.needs.append(TaskPropertyNeeded(property=need))
        task.license = tinfo.license
        task.owner = tinfo.owner

        try:
            task.uploader = identity.current.user
        except identity.RequestRequiredException:
            task.uploader = User.query.get(1)

        task.valid = True

        return task

    def to_dict(self):
        """ return a dict of this object """
        return dict(id = self.id,
                    name = self.name,
                    rpm = self.rpm,
                    path = self.path,
                    description = self.description,
                    repo = '%s' % self.repo,
                    max_time = self.avg_time,
                    destructive = self.destructive,
                    nda = self.nda,
                    creation_date = '%s' % self.creation_date,
                    update_date = '%s' % self.update_date,
                    owner = self.owner,
                    uploader = self.uploader and self.uploader.user_name,
                    version = self.version,
                    license = self.license,
                    priority = self.priority,
                    valid = self.valid or False,
                    types = ['%s' % type.type for type in self.types],
                    excluded_osmajor = ['%s' % osmajor.osmajor for osmajor in self.excluded_osmajor],
                    excluded_arch = ['%s' % arch.arch for arch in self.excluded_arch],
                    runfor = ['%s' % package for package in self.runfor],
                    required = ['%s' % package for package in self.required],
                    bugzillas = ['%s' % bug.bugzilla_id for bug in self.bugzillas],
                   )

    def to_xml(self, pretty=False):
        task = lxml.etree.Element('task',
                                  name=self.name,
                                  creation_date=str(self.creation_date),
                                  version=str(self.version),
                                  )

        # 'destructive' and 'nda' field could be NULL if it's missing from
        # testinfo.desc. To satisfy the Relax NG schema, such attributes
        # should be omitted. So only set these attributes when they're present.
        optional_attrs = ['destructive', 'nda']
        for attr in optional_attrs:
            if getattr(self, attr) is not None:
                task.set(attr, str(getattr(self, attr)).lower())

        desc =  lxml.etree.Element('description')
        desc.text =u'%s' % self.description
        task.append(desc)

        owner = lxml.etree.Element('owner')
        owner.text = u'%s' % self.owner
        task.append(owner)

        path = lxml.etree.Element('path')
        path.text = u'%s' % self.path
        task.append(path)

        rpms = lxml.etree.Element('rpms')
        rpms.append(lxml.etree.Element('rpm',
                                       url=absolute_url('/rpms/%s' % self.rpm),
                                       name=u'%s' % self.rpm))
        task.append(rpms)
        if self.bugzillas:
            bzs = lxml.etree.Element('bugzillas')
            for bz in self.bugzillas:
                bz_elem = lxml.etree.Element('bugzilla')
                bz_elem.text = str(bz.bugzilla_id)
                bzs.append(bz_elem)
            task.append(bzs)
        if self.runfor:
            runfor = lxml.etree.Element('runFor')
            for package in self.runfor:
                package_elem = lxml.etree.Element('package')
                package_elem.text = package.package
                runfor.append(package_elem)
            task.append(runfor)
        if self.required:
            requires = lxml.etree.Element('requires')
            for required in self.required:
                required_elem = lxml.etree.Element('package')
                required_elem.text = required.package
                requires.append(required_elem)
            task.append(requires)
        if self.types:
            types = lxml.etree.Element('types')
            for type in self.types:
                type_elem = lxml.etree.Element('type')
                type_elem.text = type.type
                types.append(type_elem)
            task.append(types)
        if self.excluded_osmajor:
            excluded = lxml.etree.Element('excludedDistroFamilies')
            for excluded_osmajor in self.excluded_osmajor:
                osmajor_elem = lxml.etree.Element('distroFamily')
                osmajor_elem.text = excluded_osmajor.osmajor.osmajor
                excluded.append(osmajor_elem)
            task.append(excluded)
        if self.excluded_arch:
            excluded = lxml.etree.Element('excludedArches')
            for excluded_arch in self.excluded_arch:
                arch_elem = lxml.etree.Element('arch')
                arch_elem.text=excluded_arch.arch.arch
                excluded.append(arch_elem)
            task.append(excluded)
        return lxml.etree.tostring(task, pretty_print=pretty)

    def elapsed_time(self, suffixes=(' year',' week',' day',' hour',' minute',' second'), add_s=True, separator=', '):
        """
        Takes an amount of seconds and turns it into a human-readable amount of 
        time.
        """
        seconds = self.avg_time
        # the formatted time string to be returned
        time = []

        # the pieces of time to iterate over (days, hours, minutes, etc)
        # - the first piece in each tuple is the suffix (d, h, w)
        # - the second piece is the length in seconds (a day is 60s * 60m * 24h)
        parts = [(suffixes[0], 60 * 60 * 24 * 7 * 52),
                (suffixes[1], 60 * 60 * 24 * 7),
                (suffixes[2], 60 * 60 * 24),
                (suffixes[3], 60 * 60),
                (suffixes[4], 60),
                (suffixes[5], 1)]

        # for each time piece, grab the value and remaining seconds, 
        # and add it to the time string
        for suffix, length in parts:
            value = seconds / length
            if value > 0:
                seconds = seconds % length
                time.append('%s%s' % (str(value),
                            (suffix, (suffix, suffix + 's')[value > 1])[add_s]))
            if seconds < 1:
                break

        return separator.join(time)

    def disable(self):
        """
        Disable task so it can't be used.
        """
        self.library.unlink_rpm(self.rpm)
        self.valid = False
        return


class TaskExcludeOSMajor(MappedObject):
    """
    A task can be excluded by arch, osmajor, or osversion
                        RedHatEnterpriseLinux3, RedHatEnterpriseLinux4
    """
    def __cmp__(self, other):
        """ Used to compare excludes that are already stored. 
        """
        if other == "%s" % self.osmajor.osmajor or \
           other == "%s" % self.osmajor.alias:
            return 0
        else:
            return 1

class TaskExcludeArch(MappedObject):
    """
    A task can be excluded by arch
                        i386, s390
    """
    def __cmp__(self, other):
        """ Used to compare excludes that are already stored. 
        """
        if other == "%s" % self.arch.arch:
            return 0
        else:
            return 1

class TaskType(MappedObject):
    """
    A task can be classified into serveral task types which can be used to
    select tasks for batch runs
    """
    @classmethod
    def by_name(cls, type):
        return cls.query.filter_by(type=type).one()


class TaskPackage(MappedObject):
    """
    A list of packages that a tasks should be run for.
    """
    @classmethod
    def by_name(cls, package):
        return cls.query.filter_by(package=package).one()

    def __repr__(self):
        return self.package

    def to_xml(self):
        package = xmldoc.createElement("package")
        package.setAttribute("name", "%s" % self.package)
        return package

class TaskPropertyNeeded(MappedObject):
    """
    Tasks can have requirements on the systems that they run on.
         *not currently implemented*
    """
    pass


class TaskBugzilla(MappedObject):
    """
    Bugzillas that apply to this Task.
    """
    pass

class Reservation(MappedObject): pass

class SystemStatusDuration(MappedObject): pass

class CallbackAttributeExtension(AttributeExtension):
    def set(self, state, value, oldvalue, initiator):
        instance = state.obj()
        if instance.callback:
            try:
                modname, _dot, funcname = instance.callback.rpartition(".")
                module = import_module(modname)
                cb = getattr(module, funcname)
                cb(instance, value)
            except Exception, e:
                log.error("command callback failed: %s" % e)
        return value

class VirtManager(object):

    def __init__(self):
        self.api = None

    def __enter__(self):
        self.api = ovirtsdk.api.API(url=get('ovirt.api_url'), timeout=10,
                username=get('ovirt.username'), password=get('ovirt.password'),
                # XXX add some means to specify SSL CA cert
                insecure=True)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.api, api = None, self.api
        api.disconnect()

    def create_vm(self, name, lab_controllers, *args, **kwargs):
        if self.api is None:
            raise RuntimeError('Context manager was not entered')

        # Try to create the VM on every cluster that is in an acceptable data center
        for lab_controller in lab_controllers:
            for mapping in lab_controller.data_centers:
                cluster_query = 'datacenter.name=%s' % mapping.data_center
                clusters = self.api.clusters.list(cluster_query)
                if mapping.storage_domain:
                    storage_domains = [self.api.storagedomains.get(mapping.storage_domain)]
                else:
                    sd_query = 'datacenter=%s' % mapping.data_center
                    storage_domains = self.api.storagedomains.list(sd_query)
                for cluster in clusters:
                    log.debug('Trying to create vm %s on cluster %s', name, cluster.name)
                    vm = None
                    try:
                        self._create_vm_on_cluster(name, cluster,
                                storage_domains, *args, **kwargs)
                    except Exception:
                        log.exception("Failed to create VM %s on cluster %s",
                                name, cluster.name)
                        if vm is not None:
                            try:
                                vm.delete()
                            except Exception:
                                pass
                        continue
                    else:
                        return lab_controller
        raise VMCreationFailedException('No clusters successfully created VM %s' % name)

    def _create_vm_on_cluster(self, name, cluster, storage_domains,
            mac_address=None, virtio_possible=True):
        from ovirtsdk.xml.params import VM, Template, NIC, Network, Disk, \
                StorageDomains, MAC
        # Default of 1GB memory and 20GB disk
        memory = ConfigItem.by_name(u'default_guest_memory').current_value(1024) * 1024**2
        disk_size = ConfigItem.by_name(u'default_guest_disk_size').current_value(20) * 1024**3

        if virtio_possible:
            nic_interface = "virtio"
            disk_interface = "virtio"
        else:
            # use emulated interface
            nic_interface = "rtl8139"
            disk_interface = "ide"

        vm_definition = VM(name=name, memory=memory, cluster=cluster,
                type_='server', template=Template(name='Blank'))
        vm = self.api.vms.add(vm_definition)
        nic = NIC(name='eth0', interface=nic_interface, network=Network(name='rhevm'),
                mac=MAC(address=str(mac_address)))
        vm.nics.add(nic)
        disk = Disk(storage_domains=StorageDomains(storage_domain=storage_domains),
                size=disk_size, type_='data', interface=disk_interface, format='cow',
                bootable=True)
        vm.disks.add(disk)

        # Wait up to twenty seconds(!) for the disk image to be created.
        # Check both the vm state and disk state, image creation may not
        # lock the vms. That should be a RHEV issue, but it doesn't hurt
        # to check both of them by us.
        for _ in range(20):
            vm = self.api.vms.get(name)
            vm_state = vm.status.state
            # create disk with param 'name' doesn't work in RHEV 3.0, so just
            # find the first disk of the vm as we only attached one to it
            disk_state = vm.disks.list()[0].status.state
            if vm_state == 'down' and disk_state == "ok":
                break
            time.sleep(1)
        vm = self.api.vms.get(name)
        vm_state = vm.status.state
        disk_state = vm.disks.list()[0].status.state
        if vm_state != 'down':
            raise ValueError("VM %s's state: %s", name, vm_state)
        if disk_state != 'ok':
            raise ValueError("VM %s's disk state: %s", name, disk_state)

    def start_install(self, name, distro_tree, kernel_options, lab_controller):
        if self.api is None:
            raise RuntimeError('Context manager was not entered')
        from ovirtsdk.xml.params import OperatingSystem, Action, VM
        # RHEV can only handle a local path to kernel/initrd, so we rely on autofs for now :-(
        # XXX when this constraint is lifted, fix beakerd.virt_recipes too
        location = distro_tree.url_in_lab(lab_controller, 'nfs', required=True)
        kernel = distro_tree.image_by_type(ImageType.kernel, KernelType.by_name(u'default'))
        initrd = distro_tree.image_by_type(ImageType.initrd, KernelType.by_name(u'default'))
        local_path = location.replace('nfs://', '/net/', 1).replace(':/', '/', 1)
        kernel_path = os.path.join(local_path, kernel.path)
        initrd_path = os.path.join(local_path, initrd.path)
        log.debug(u'Starting VM %s installing %s', name, distro_tree)
        a = Action(vm=VM(os=OperatingSystem(kernel=kernel_path,
                initrd=initrd_path, cmdline=kernel_options)))
        self.api.vms.get(name).start(action=a)

    def destroy_vm(self, name):
        from ovirtsdk.infrastructure.errors import RequestError
        if self.api is None:
            raise RuntimeError('Context manager was not entered')
        vm = self.api.vms.get(name)
        if vm is not None:
            try:
                log.debug('Stopping %s on %r', name, self)
                vm.stop()
            except RequestError:
                pass # probably not running for some reason
            log.debug('Deleting %s on %r', name, self)
            vm.delete()


class ExternalReport(DeclBase, MappedObject):

    __tablename__ = 'external_reports'
    __table_args__ = {'mysql_engine':'InnoDB'}

    id = Column(Integer, primary_key=True)
    name = Column(Unicode(100), unique=True, nullable=False)
    url = Column(Unicode(10000), nullable=False)
    description = Column(Unicode(1000), default=None)

    def __init__(self, *args, **kw):
        super(ExternalReport, self).__init__(*args, **kw)

Hypervisor.mapper = mapper(Hypervisor, hypervisor_table)
System.mapper = mapper(System, system_table,
                   properties = {
                     'status': column_property(system_table.c.status,
                        extension=SystemStatusAttributeExtension()),
                     'devices':relation(Device,
                                        secondary=system_device_map,backref='systems'),
                     'disks':relation(Disk, backref='system',
                        cascade='all, delete, delete-orphan'),
                     'arch':relation(Arch,
                                     order_by=[arch_table.c.arch],
                                        secondary=system_arch_map,
                                        backref='systems'),
                     'labinfo':relation(LabInfo, uselist=False, backref='system',
                        cascade='all, delete, delete-orphan'),
                     'cpu':relation(Cpu, uselist=False,backref='systems',
                        cascade='all, delete, delete-orphan'),
                     'numa':relation(Numa, uselist=False, backref='system',
                        cascade='all, delete, delete-orphan'),
                     'power':relation(Power, uselist=False, backref='system',
                        cascade='all, delete, delete-orphan'),
                     'excluded_osmajor':relation(ExcludeOSMajor, backref='system',
                        cascade='all, delete, delete-orphan'),
                     'excluded_osversion':relation(ExcludeOSVersion, backref='system',
                        cascade='all, delete, delete-orphan'),
                     'provisions':relation(Provision, collection_class=attribute_mapped_collection('arch'),
                                                 backref='system', cascade='all, delete, delete-orphan'),
                     'loaned':relation(User, uselist=False,
                          primaryjoin=system_table.c.loan_id==users_table.c.user_id,foreign_keys=system_table.c.loan_id),
                     'user':relation(User, uselist=False,
                          primaryjoin=system_table.c.user_id==users_table.c.user_id,foreign_keys=system_table.c.user_id),
                     'owner':relation(User, uselist=False,
                          primaryjoin=system_table.c.owner_id==users_table.c.user_id,foreign_keys=system_table.c.owner_id),
                     'group_assocs': relation(SystemGroup, cascade='all, delete-orphan', backref='system'),
                     'lab_controller':relation(LabController, uselist=False,
                                               backref='systems'),
                     'notes':relation(Note,
                                      order_by=[note_table.c.created.desc()],
                                      cascade="all, delete, delete-orphan"),
                     'key_values_int':relation(Key_Value_Int,
                                      cascade="all, delete, delete-orphan",
                                                backref='system'),
                     'key_values_string':relation(Key_Value_String,
                                      cascade="all, delete, delete-orphan",
                                                backref='system'),
                     'activity':relation(SystemActivity,
                        order_by=[activity_table.c.created.desc(), activity_table.c.id.desc()],
                        backref='object', cascade='all, delete'),
                     'dyn_activity': dynamic_loader(SystemActivity,
                        order_by=[activity_table.c.created.desc(), activity_table.c.id.desc()]),
                     'command_queue':relation(CommandActivity,
                        order_by=[activity_table.c.created.desc(), activity_table.c.id.desc()],
                        backref='object', cascade='all, delete, delete-orphan'),
                     'dyn_command_queue': dynamic_loader(CommandActivity),
                     'reprovision_distro_tree':relation(DistroTree, uselist=False),
                      '_system_ccs': relation(SystemCc, backref='system',
                                      cascade="all, delete, delete-orphan"),
                     'reservations': relation(Reservation, backref='system',
                        order_by=[reservation_table.c.start_time.desc()]),
                     'dyn_reservations': dynamic_loader(Reservation),
                     'open_reservation': relation(Reservation, uselist=False, viewonly=True,
                        primaryjoin=and_(system_table.c.id == reservation_table.c.system_id,
                            reservation_table.c.finish_time == None)),
                     'status_durations': relation(SystemStatusDuration, backref='system',
                        cascade='all, delete, delete-orphan',
                        order_by=[system_status_duration_table.c.start_time.desc(),
                                  system_status_duration_table.c.id.desc()]),
                     'dyn_status_durations': dynamic_loader(SystemStatusDuration),
                     'hypervisor':relation(Hypervisor, uselist=False),
                     'kernel_type':relation(KernelType, uselist=False),
                     # The relationship to 'recipe' is complicated
                     # by the polymorphism of SystemResource :-(
                     'recipes': relation(Recipe, viewonly=True,
                        secondary=recipe_resource_table.join(system_resource_table),
                        secondaryjoin=and_(system_resource_table.c.id == recipe_resource_table.c.id,
                            recipe_resource_table.c.recipe_id == recipe_table.c.id)),
                     'dyn_recipes': dynamic_loader(Recipe,
                        secondary=recipe_resource_table.join(system_resource_table),
                        secondaryjoin=and_(system_resource_table.c.id == recipe_resource_table.c.id,
                            recipe_resource_table.c.recipe_id == recipe_table.c.id)),
                     })

mapper(SystemCc, system_cc_table)
mapper(SystemStatusDuration, system_status_duration_table)

Cpu.mapper = mapper(Cpu, cpu_table, properties={
    'flags': relation(CpuFlag, cascade='all, delete, delete-orphan'),
    'system': relation(System),
})
mapper(Provision, provision_table,
       properties = {'provision_families':relation(ProvisionFamily,
            collection_class=attribute_mapped_collection('osmajor'),
            cascade='all, delete, delete-orphan'),
                     'arch':relation(Arch)})
mapper(ProvisionFamily, provision_family_table,
       properties = {'provision_family_updates':relation(ProvisionFamilyUpdate,
            collection_class=attribute_mapped_collection('osversion'),
            cascade='all, delete, delete-orphan'),
                     'osmajor':relation(OSMajor)})
mapper(ProvisionFamilyUpdate, provision_family_update_table,
       properties = {'osversion':relation(OSVersion)})
mapper(ExcludeOSMajor, exclude_osmajor_table,
       properties = {'osmajor':relation(OSMajor, backref='excluded_osmajors'),
                     'arch':relation(Arch)})
mapper(ExcludeOSVersion, exclude_osversion_table,
       properties = {'osversion':relation(OSVersion, backref='excluded_osversions'),
                     'arch':relation(Arch)})
mapper(LabInfo, labinfo_table)
mapper(Watchdog, watchdog_table,
       properties = {'recipetask':relation(RecipeTask, uselist=False),
                     'recipe':relation(Recipe, uselist=False,
                                      )})
CpuFlag.mapper = mapper(CpuFlag, cpu_flag_table)
Numa.mapper = mapper(Numa, numa_table)
Device.mapper = mapper(Device, device_table,
       properties = {'device_class': relation(DeviceClass)})
mapper(DeviceClass, device_class_table)
mapper(Disk, disk_table)
mapper(PowerType, power_type_table)
mapper(Power, power_table,
        properties = {'power_type':relation(PowerType,
                                           backref='power_control')
    })

mapper(BeakerTag, beaker_tag_table,
        polymorphic_on=beaker_tag_table.c.type, polymorphic_identity=u'tag')

mapper(RetentionTag, retention_tag_table, inherits=BeakerTag, 
        properties=dict(is_default=retention_tag_table.c.default_),
        polymorphic_identity=u'retention_tag')

mapper(SystemActivity, system_activity_table, inherits=Activity,
        polymorphic_identity=u'system_activity')

mapper(RecipeSetActivity, recipeset_activity_table, inherits=Activity,
       polymorphic_identity=u'recipeset_activity')

mapper(CommandActivity, command_queue_table, inherits=Activity,
       polymorphic_identity=u'command_activity',
       properties={'status': column_property(command_queue_table.c.status,
                        extension=CallbackAttributeExtension()),
                   'system':relation(System),
                   'distro_tree': relation(DistroTree),
                  })

mapper(Note, note_table,
        properties=dict(user=relation(User, uselist=False,
                        backref='notes')))

Key.mapper = mapper(Key, key_table)
           
mapper(Key_Value_Int, key_value_int_table, properties={
        'key': relation(Key, uselist=False,
            backref=backref('key_value_int', cascade='all, delete-orphan'))
        })
mapper(Key_Value_String, key_value_string_table, properties={
        'key': relation(Key, uselist=False,
            backref=backref('key_value_string', cascade='all, delete-orphan'))
        })

mapper(Task, task_table,
        properties = {'types':relation(TaskType,
                                        secondary=task_type_map,
                                        backref='tasks'),
                      'excluded_osmajor':relation(TaskExcludeOSMajor,
                                        backref='task'),
                      'excluded_arch':relation(TaskExcludeArch,
                                        backref='task'),
                      'runfor':relation(TaskPackage,
                                        secondary=task_packages_runfor_map,
                                        backref='tasks'),
                      'required':relation(TaskPackage,
                                        secondary=task_packages_required_map,
                                        order_by=[task_package_table.c.package]),
                      'needs':relation(TaskPropertyNeeded),
                      'bugzillas':relation(TaskBugzilla, backref='task',
                                            cascade='all, delete-orphan'),
                      'uploader':relation(User, uselist=False, backref='tasks'),
                     }
      )

mapper(TaskExcludeOSMajor, task_exclude_osmajor_table,
       properties = {
                     'osmajor':relation(OSMajor),
                    }
      )

mapper(TaskExcludeArch, task_exclude_arch_table,
       properties = {
                     'arch':relation(Arch),
                    }
      )

mapper(TaskPackage, task_package_table)
mapper(TaskPropertyNeeded, task_property_needed_table)
mapper(TaskType, task_type_table)
mapper(TaskBugzilla, task_bugzilla_table)

mapper(Job, job_table,
        properties = {'recipesets':relation(RecipeSet, backref='job'),
                      'owner':relation(User, uselist=False,
                          backref=backref('jobs', cascade_backrefs=False),
                          primaryjoin=users_table.c.user_id ==  \
                          job_table.c.owner_id, foreign_keys=job_table.c.owner_id),
                      'submitter': relation(User, uselist=False,
                          primaryjoin=users_table.c.user_id == \
                          job_table.c.submitter_id),
                      'group': relation(Group, uselist=False,
                          backref=backref('jobs', cascade_backrefs=False)),
                      'retention_tag':relation(RetentionTag, uselist=False,
                          backref=backref('jobs', cascade_backrefs=False)),
                      'product':relation(Product, uselist=False,
                          backref=backref('jobs', cascade_backrefs=False)),
                      '_job_ccs': relation(JobCc, backref='job')})

mapper(JobCc, job_cc_table)

mapper(Product, product_table)

mapper(RecipeSetResponse,recipe_set_nacked_table,
        properties = { 'recipesets':relation(RecipeSet),
                        'response' : relation(Response,uselist=False)})

mapper(Response,response_table)

mapper(RecipeSet, recipe_set_table,
        properties = {'recipes':relation(Recipe, backref='recipeset'),
                      'activity':relation(RecipeSetActivity,
                        order_by=[activity_table.c.created.desc(), activity_table.c.id.desc()],
                        backref='object'),
                      'lab_controller':relation(LabController, uselist=False),
                      'nacked':relation(RecipeSetResponse,cascade="all, delete-orphan",uselist=False),
                     })

mapper(LogRecipe, log_recipe_table)

mapper(LogRecipeTask, log_recipe_task_table)

mapper(LogRecipeTaskResult, log_recipe_task_result_table)

mapper(Recipe, recipe_table,
        polymorphic_on=recipe_table.c.type, polymorphic_identity=u'recipe',
        properties = {'distro_tree':relation(DistroTree, uselist=False,
                        backref=backref('recipes', cascade_backrefs=False)),
                      'resource': relation(RecipeResource, uselist=False,
                                        backref='recipe'),
                      'rendered_kickstart': relation(RenderedKickstart),
                      'watchdog':relation(Watchdog, uselist=False,
                                         cascade="all, delete, delete-orphan"),
                      'systems':relation(System, 
                                         secondary=system_recipe_map,
                                         backref='queued_recipes'),
                      'dyn_systems':dynamic_loader(System,
                                         secondary=system_recipe_map,
                                         primaryjoin=recipe_table.c.id==system_recipe_map.c.recipe_id,
                                         secondaryjoin=system_table.c.id==system_recipe_map.c.system_id,
                      ),
                      'tasks':relation(RecipeTask, backref='recipe'),
                      'dyn_tasks': relation(RecipeTask, lazy='dynamic'),
                      'tags':relation(RecipeTag, 
                                      secondary=recipe_tag_map,
                                      backref='recipes'),
                      'repos':relation(RecipeRepo),
                      'rpms':relation(RecipeRpm, backref='recipe'),
                      'logs':relation(LogRecipe, backref='parent',
                            cascade='all, delete-orphan'),
                      'custom_packages':relation(TaskPackage,
                                        secondary=task_packages_custom_map),
                      'ks_appends':relation(RecipeKSAppend),
                     }
      )
mapper(GuestRecipe, guest_recipe_table, inherits=Recipe,
        polymorphic_identity=u'guest_recipe')
mapper(MachineRecipe, machine_recipe_table, inherits=Recipe,
        polymorphic_identity=u'machine_recipe',
        properties = {'guests':relation(Recipe, backref=backref('hostrecipe', uselist=False),
                                        secondary=machine_guest_map)})

mapper(RecipeResource, recipe_resource_table,
        polymorphic_on=recipe_resource_table.c.type, polymorphic_identity=None,)
mapper(SystemResource, system_resource_table, inherits=RecipeResource,
        polymorphic_on=recipe_resource_table.c.type, polymorphic_identity=ResourceType.system,
        properties={
            'system': relation(System),
            'reservation': relation(Reservation, uselist=False),
        })
mapper(VirtResource, virt_resource_table, inherits=RecipeResource,
        polymorphic_on=recipe_resource_table.c.type, polymorphic_identity=ResourceType.virt,
        properties={
            'lab_controller': relation(LabController),
        })
mapper(GuestResource, guest_resource_table, inherits=RecipeResource,
        polymorphic_on=recipe_resource_table.c.type, polymorphic_identity=ResourceType.guest)

mapper(RecipeTag, recipe_tag_table)
mapper(RecipeRpm, recipe_rpm_table)
mapper(RecipeRepo, recipe_repo_table)
mapper(RecipeKSAppend, recipe_ksappend_table)

mapper(RecipeTask, recipe_task_table,
        properties = {'results':relation(RecipeTaskResult, 
                                         backref='recipetask'),
                      'rpms':relation(RecipeTaskRpm),
                      'comments':relation(RecipeTaskComment, 
                                          backref='recipetask'),
                      'params':relation(RecipeTaskParam),
                      'bugzillas':relation(RecipeTaskBugzilla, 
                                           backref='recipetask'),
                      'task':relation(Task, uselist=False),
                      'logs':relation(LogRecipeTask, backref='parent',
                            cascade='all, delete-orphan'),
                      'watchdog':relation(Watchdog, uselist=False),
                     }
      )

mapper(RecipeTaskParam, recipe_task_param_table)
mapper(RecipeTaskComment, recipe_task_comment_table,
        properties = {'user':relation(User, uselist=False, backref='comments')})
mapper(RecipeTaskBugzilla, recipe_task_bugzilla_table)
mapper(RecipeTaskRpm, recipe_task_rpm_table)
mapper(RecipeTaskResult, recipe_task_result_table,
        properties = {'logs':relation(LogRecipeTaskResult, backref='parent',
                           cascade='all, delete-orphan'),
                     }
      )
mapper(RenderedKickstart, rendered_kickstart_table)
mapper(Reservation, reservation_table, properties={
        'user': relation(User, backref=backref('reservations',
            order_by=[reservation_table.c.start_time.desc()])),
        # The relationship to 'recipe' is complicated
        # by the polymorphism of SystemResource :-(
        'recipe': relation(Recipe, uselist=False, viewonly=True,
            secondary=recipe_resource_table.join(system_resource_table),
            secondaryjoin=and_(system_resource_table.c.id == recipe_resource_table.c.id,
                recipe_resource_table.c.recipe_id == recipe_table.c.id)),
})

class_mapper(LabController).add_property('dyn_systems', dynamic_loader(System))

## Static list of device_classes -- used by master.kid
_device_classes = None
def device_classes():
    global _device_classes
    if not _device_classes:
        _device_classes = DeviceClass.query.all()
    for device_class in _device_classes:
        yield device_class

# available in python 2.7+ importlib
def import_module(modname):
    __import__(modname)
    return sys.modules[modname]

def auto_cmd_handler(command, new_status):
    if not command.system.open_reservation:
        return
    recipe = command.system.open_reservation.recipe
    if new_status in (CommandStatus.failed, CommandStatus.aborted):
        recipe.abort("Command %s failed" % command.id)
    elif command.action == u'reboot':
        recipe.resource.rebooted = datetime.utcnow()
        first_task = recipe.first_task
        first_task.start()