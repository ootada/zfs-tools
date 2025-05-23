# zbackup - property driven ZFS backup utility, using zsnap/zreplicate.
#
# Author: Simon Guest, 11/4/2014
# Licensed under GNU General Public License GPLv3

import optparse
import smtplib
import socket
import subprocess
import sys
from email.mime.text import MIMEText

from zfs_tools.util import stderr, verbose_stderr, set_verbose

# ZFS user property module prefix
ZBACKUP_MODULE = "com.github.tesujimath.zbackup"
ZBACKUP_MODULE_SKIPLEN = len(ZBACKUP_MODULE) + 1

# property names
REPLICA_PROPERTY = "replica"
REPLICATE_PROPERTY = "replicate"
SNAPSHOTS_PROPERTY_SUFFIX = "-snapshots"
SNAPSHOT_LIMIT_PROPERTY_SUFFIX = "-snapshot-limit"


def highlight(line):
    """Highlight a line in the output."""
    return "========== %s ==========" % line


def zprefixed(prop):
    """Return property with <ZBACKUP_MODULE> prefix."""
    return "%s:%s" % (ZBACKUP_MODULE, prop)


def is_zprefixed(maybe_prefixed_property):
    """Return whether property has <ZBACKUP_MODULE> prefix."""
    return maybe_prefixed_property.startswith("%s:" % ZBACKUP_MODULE)


def zunprefixed(prefixed_property):
    """Return property with prefix stripped."""
    return prefixed_property[ZBACKUP_MODULE_SKIPLEN:]


def snapshots_property(tier):
    """Return barename of snapshots property for given tier."""
    return "%s%s" % (tier, SNAPSHOTS_PROPERTY_SUFFIX)


def snapshot_limit_property(tier):
    """Return barename of snapshot-limit property for given tier."""
    return "%s%s" % (tier, SNAPSHOT_LIMIT_PROPERTY_SUFFIX)


def zbackup_properties(tier):
    """Return list of relevant properties for given tier, unprefixed."""
    return [
        REPLICA_PROPERTY,
        REPLICATE_PROPERTY,
        snapshots_property(tier),
        snapshot_limit_property(tier),
    ]


def get_zpools():
    """Return list of zpools."""
    zpools = []
    zpool_list = subprocess.Popen(["zpool", "list", "-H"], stdout=subprocess.PIPE)
    for line in zpool_list.stdout:
        zpools.append(line.split()[0])
    return zpools


def get_backup_properties(zpool, options, tier=None):
    """Return the backup of all filesystems, by scanning the filesystems for relevant user properties.
    Only locally set and received properties are used;  inherited properties are ignored."""
    properties = {}
    if tier != None:
        property_ids = ",".join([zprefixed(prop) for prop in zbackup_properties(tier)])
    else:
        property_ids = "all"
    cmd = ["zfs", "get", "-H", "-r", "-t", "filesystem", property_ids, zpool]
    zfs_get = subprocess.Popen(cmd, stdout=subprocess.PIPE, universal_newlines=True)
    for line in zfs_get.stdout:
        (name, prop, value, source) = line.rstrip("\n").split("\t")
        if is_zprefixed(prop):
            bare_property = zunprefixed(prop)
            if source.startswith("inherited"):
                source = "inherited"
            if (
                source == "local" or source == "received"
            ):  # not this for now: or (source == 'inherited' and bare_property == snapshot_limit_property(tier)):
                if not name in properties:
                    properties[name] = {}
                if value != "-":
                    properties[name][bare_property] = (value, source)
                    verbose_stderr("%s %s=%s %s" % (name, bare_property, value, source))
    retcode = zfs_get.wait()
    if retcode != 0:
        raise subprocess.CalledProcessError(retcode, cmd)
    return properties


def snapshot(tier, filesystem, take_snapshot, keep, options):
    """Snapshot given filesystem."""
    zsnap_command = ["zsnap", "-k", str(keep), "-p", ("%s%s-" % (options.prefix, tier))]
    if not take_snapshot:
        zsnap_command += ["--nosnapshot"]
    if options.verbose:
        zsnap_command += ["-v"]
    if options.timeformat != None:
        zsnap_command += ["-t", options.timeformat]
    if options.dryrun:
        zsnap_command += ["-n"]
    if options.zsnap_options != None:
        zsnap_command += options.zsnap_options.split()
    zsnap_command += [filesystem]
    verbose_stderr("%s" % highlight(" ".join(zsnap_command)))
    subprocess.check_call(zsnap_command)


def replicate(filesystem, destination, options):
    """Replicate given filesystem, possibly after deleting snapshots from other tiers."""
    # delete other tiers snapshots first?
    if options.delete_tiers != None:
        for tier in options.delete_tiers.split(","):
            snapshot(tier, filesystem, False, 0, options)
    # replicate
    zreplicate_command = [
        "zreplicate",
        "--create-destination",
        "--no-replication-stream",
    ]
    if options.verbose:
        zreplicate_command += ["-v"]
    if options.dryrun:
        zreplicate_command += ["-n"]
    if options.zreplicate_options != None:
        zreplicate_command += options.zreplicate_options.split()
    zreplicate_command += [filesystem, destination]
    verbose_stderr("%s" % highlight(" ".join(zreplicate_command)))
    subprocess.check_call(zreplicate_command)


def property_has_value(properties, property_name):
    """Return whether the property has a (not none) value."""
    if property_name in properties:
        (value, source) = properties[property_name]
        return value != "none"
    else:
        return False


def property_int_value_or_none(filesystem, properties, property_name):
    """Interpret the property value as an integer value, or None."""
    (value, source) = (None, None)
    if property_has_value(properties, property_name):
        (value_s, source) = properties[property_name]
        try:
            value = int(value_s)
        except ValueError:
            stderr(
                "badly formed %s=%s property for %s (should be integer)"
                % (property_name, properties[property_name], filesystem)
            )
    return (value, source)


def backup_or_reap_snapshots(tier, filesystem, properties, options):
    """Backup and/or reap snapshots for the given filesystem, as per the given properties and options."""
    # snapshot?
    take_snapshot = False
    # snapshots property: we only take a snapshot if the property source is local.
    # If the property source is received, we use the value to reap old snapshots.
    (snapshots, snapshots_source) = property_int_value_or_none(
        filesystem, properties, snapshots_property(tier)
    )
    if snapshots != None and snapshots_source == "local":
        take_snapshot = True
    # snapshot-limit property, overrides value of snapshots if both present
    (snapshot_limit, snapshot_limit_source) = property_int_value_or_none(
        filesystem, properties, snapshot_limit_property(tier)
    )
    if snapshot_limit != None:
        snapshots = snapshot_limit
    if snapshots != None:
        snapshot(tier, filesystem, take_snapshot, snapshots, options)

    # replicate? - only if the source of both properties is local
    if (
        property_has_value(properties, REPLICATE_PROPERTY)
        and properties[REPLICATE_PROPERTY] == (tier, "local")
        and property_has_value(properties, REPLICA_PROPERTY)
    ):
        (replica, replica_source) = properties[REPLICA_PROPERTY]
        if replica_source == "local":
            for x in replica.split(","):
                replicate(filesystem, replica, options)


def format_backup_properties(properties):
    """Format the properties to show what will be done by zbackup.

    Note that the handling of backup properties with different sources is carefully designed - see the logic in backup_or_reap_snapshots() above."""
    # split properties according to source
    local_and_defaults = {}
    non_local = {}
    for name in properties.keys():
        if properties[name][1] == "local":
            local_and_defaults[name] = properties[name]
        else:
            non_local[name] = properties[name]
    # non_local snapshot properties provide defaults for snapshot-limit if not set locally
    for name in non_local.keys():
        snapshot_tier = None
        if name.endswith(SNAPSHOTS_PROPERTY_SUFFIX):
            snapshot_tier = name[: -len(SNAPSHOTS_PROPERTY_SUFFIX)]
        elif name.endswith(SNAPSHOT_LIMIT_PROPERTY_SUFFIX):
            snapshot_tier = name[: -len(SNAPSHOT_LIMIT_PROPERTY_SUFFIX)]
        if (
            snapshot_tier != None
            and not snapshot_limit_property(snapshot_tier) in local_and_defaults
        ):
            local_and_defaults[snapshot_limit_property(snapshot_tier)] = non_local[name]

    names = [
        name
        for name in sorted(local_and_defaults.keys())
        if name not in [REPLICA_PROPERTY, REPLICATE_PROPERTY]
    ] + [
        name
        for name in sorted(local_and_defaults.keys())
        if name in [REPLICA_PROPERTY, REPLICATE_PROPERTY]
    ]
    return " ".join(["%s=%s" % (name, local_and_defaults[name][0]) for name in names])


def list_backup_properties(options):
    # get properties for all tiers
    for zpool in get_zpools():
        backup_properties = get_backup_properties(zpool, options)
        for filesystem in sorted(backup_properties.keys()):
            print(
                "%s %s"
                % (filesystem, format_backup_properties(backup_properties[filesystem]))
            )


def set_backup_properties(filesystem, property_values):
    for property_value in property_values:
        toks = property_value.split("=")
        if len(toks) == 2:
            prop, value = toks
            zfs_command = ["zfs", "set", "%s=%s" % (zprefixed(prop), value), filesystem]
            sys.stdout.write("%s\n" % " ".join(zfs_command))
            subprocess.check_call(zfs_command)
        else:
            sys.stderr.write(
                "zbackup: ignoring badly formatted property=value: %s\n"
                % property_value
            )


def unset_backup_properties(filesystem, properties):
    for prop in properties:
        zfs_command = ["zfs", "inherit", zprefixed(prop), filesystem]
        sys.stdout.write("%s\n" % " ".join(zfs_command))
        subprocess.check_call(zfs_command)


def backup_by_properties(tier, options):
    for zpool in get_zpools():
        backup_properties = get_backup_properties(zpool, options, tier)
        for filesystem in backup_properties.keys():
            backup_or_reap_snapshots(
                tier, filesystem, backup_properties[filesystem], options
            )


def send_failure_email(recipient, message):
    """Email recipient with failure message."""
    hostname = socket.gethostname()
    sender = "root@%s" % hostname
    msg = MIMEText(message)
    msg["Subject"] = "zbackup failed on %s" % hostname
    msg["From"] = sender
    msg["To"] = recipient
    s = smtplib.SMTP("localhost")
    s.sendmail(sender, [recipient], msg.as_string())
    s.quit()


def main():
    usage = "usage: %prog [options] [<tier>] [<property=value>] [<property>]"
    parser = optparse.OptionParser(usage)
    parser.add_option(
        "-d",
        "--delete-tiers",
        action="store",
        dest="delete_tiers",
        default=None,
        help="comma-separated snapshot tiers to delete (default: %default)",
    )
    parser.add_option(
        "-p",
        "--prefix",
        action="store",
        dest="prefix",
        default="auto-",
        help="prefix to prepend to tier in snapshot names (default: %default)",
    )
    parser.add_option(
        "-v",
        "--verbose",
        action="store_true",
        dest="verbose",
        default=False,
        help="be verbose (default: %default)",
    )
    parser.add_option(
        "-e",
        "--email-on-failure",
        action="store",
        dest="email_failure",
        metavar="RECIPIENT_ADDRESS",
        default=None,
        help="email recipient on failure (default: None)",
    )
    parser.add_option(
        "-t",
        "--timeformat",
        action="store",
        dest="timeformat",
        default=None,
        help="postfix time format to append to snapshot names (default: as per zsnap)",
    )
    parser.add_option(
        "-n",
        "--dry-run",
        action="store_true",
        dest="dryrun",
        default=False,
        help="don't actually manipulate any file systems",
    )
    parser.add_option(
        "-l",
        "--list",
        action="store_true",
        dest="list",
        default=False,
        help="list backup properties, do nothing else",
    )
    parser.add_option(
        "-s",
        "--set",
        action="store_true",
        dest="set",
        default=False,
        help="set backup properties, do nothing else",
    )
    parser.add_option(
        "-u",
        "--unset",
        action="store_true",
        dest="unset",
        default=False,
        help="unset backup properties, do nothing else",
    )
    parser.add_option(
        "--zreplicate-options",
        action="store",
        dest="zreplicate_options",
        default=None,
        type="string",
        help="options passed to zreplicate (default: %default)",
    )
    parser.add_option(
        "--zsnap-options",
        action="store",
        dest="zsnap_options",
        default=None,
        type="string",
        help="options passed to zsnap (default: %default)",
    )
    (options, args) = parser.parse_args(sys.argv)

    set_verbose(options.verbose)

    try:
        if options.list:
            # just list the backup properties
            list_backup_properties(options)
        elif options.set:
            # just set the backup properties
            if len(args) >= 3:
                set_backup_properties(args[1], args[2:])
            else:
                stderr("usage: zbackup --set <filesystem> <property=value> ...")
                sys.exit(1)
        elif options.unset:
            # just unset the backup properties
            if len(args) >= 3:
                unset_backup_properties(args[1], args[2:])
            else:
                stderr("usage: zbackup --unset <filesystem> <property> ...")
                sys.exit(1)
        else:
            if len(args) == 2:
                backup_by_properties(args[1], options)
            else:
                stderr("usage: zbackup <tier>")
                sys.exit(1)
    except Exception as e:
        # report exception and exit
        message = "zbackup failed with exception: %s" % e
        stderr(message)
        if options.email_failure != None:
            send_failure_email(options.email_failure, message)
        sys.exit(1)
