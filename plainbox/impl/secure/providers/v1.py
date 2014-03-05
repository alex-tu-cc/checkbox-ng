# This file is part of Checkbox.
#
# Copyright 2013 Canonical Ltd.
# Written by:
#   Zygmunt Krynicki <zygmunt.krynicki@canonical.com>
#
# Checkbox is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3,
# as published by the Free Software Foundation.

#
# Checkbox is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Checkbox.  If not, see <http://www.gnu.org/licenses/>.

"""
:mod:`plainbox.impl.secure.providers.v1` -- Implementation of V1 provider
=========================================================================
"""

import errno
import itertools
import logging
import os

from plainbox.abc import IProvider1, IProviderBackend1
from plainbox.i18n import gettext as _
from plainbox.impl.job import JobDefinition
from plainbox.impl.secure.config import Config, Variable
from plainbox.impl.secure.config import IValidator
from plainbox.impl.secure.config import NotEmptyValidator
from plainbox.impl.secure.config import NotUnsetValidator
from plainbox.impl.secure.config import PatternValidator
from plainbox.impl.secure.config import Unset
from plainbox.impl.secure.plugins import FsPlugInCollection
from plainbox.impl.secure.plugins import IPlugIn
from plainbox.impl.secure.plugins import PlugInError
from plainbox.impl.secure.qualifiers import WhiteList
from plainbox.impl.secure.rfc822 import FileTextSource
from plainbox.impl.secure.rfc822 import RFC822SyntaxError
from plainbox.impl.secure.rfc822 import load_rfc822_records


logger = logging.getLogger("plainbox.secure.providers.v1")


class WhiteListPlugIn(IPlugIn):
    """
    A specialized :class:`plainbox.impl.secure.plugins.IPlugIn` that loads
    :class:`plainbox.impl.secure.qualifiers.WhiteList` instances from a file.
    """

    def __init__(self, filename, text):
        """
        Initialize the plug-in with the specified name text
        """
        try:
            self._whitelist = WhiteList.from_string(text, filename=filename)
        except Exception as exc:
            raise PlugInError(
                _("Cannot load whitelist {!r}: {}").format(filename, exc))

    @property
    def plugin_name(self):
        """
        plugin name, the name of the WhiteList
        """
        return self._whitelist.name

    @property
    def plugin_object(self):
        """
        plugin object, the actual WhiteList instance
        """
        return self._whitelist


class JobDefinitionPlugIn(IPlugIn):
    """
    A specialized :class:`plainbox.impl.secure.plugins.IPlugIn` that loads a
    list of :class:`plainbox.impl.job.JobDefinition` instances from a file.
    """

    def __init__(self, filename, text, provider):
        """
        Initialize the plug-in with the specified name text
        """
        self._filename = filename
        self._job_list = []
        logger.debug(_("Loading jobs definitions from %r..."), filename)
        try:
            records = load_rfc822_records(
                text, source=FileTextSource(filename))
        except RFC822SyntaxError as exc:
            raise PlugInError(
                _("Cannot load job definitions from {!r}: {}").format(
                    filename, exc))
        for record in records:
            try:
                job = JobDefinition.from_rfc822_record(record)
            except ValueError as exc:
                raise PlugInError(
                    _("Cannot define job from record {!r}: {}").format(
                        record, exc))
            else:
                job._provider = provider
                self._job_list.append(job)
                logger.debug(_("Loaded %r"), job)

    @property
    def plugin_name(self):
        """
        plugin name, name of the file we loaded jobs from
        """
        return self._filename

    @property
    def plugin_object(self):
        """
        plugin object, a list of JobDefinition instances
        """
        return self._job_list


class Provider1(IProvider1, IProviderBackend1):
    """
    A v1 provider implementation.

    A provider is a container of jobs and whitelists. It provides additional
    meta-data and knows about location of essential directories to both load
    structured data and provide runtime information for job execution.

    Providers are normally loaded with :class:`Provider1PlugIn`, due to the
    number of fields involved in basic initialization.
    """

    def __init__(self, name, version, description, secure, gettext_domain,
                 jobs_dir, whitelists_dir, data_dir, bin_dir, locale_dir):
        """
        Initialize a provider with a set of meta-data and directories.

        :param name:
            provider name / ID

        :param version:
            provider version

        :param description:
            provider version

            This is the untranslated version of this field. Implementations may
            obtain the localized version based on the gettext_domain property.

        :param secure:
            secure bit

            When True jobs from this provider should be available via the
            trusted launcher mechanism. It should be set to True for
            system-wide installed providers.

        :param gettext_domain:
            gettext domain that contains translations for this provider

        :param jobs_dir:
            path of the directory with job definitions

        :param whitelists_dir:
            path of the directory with whitelists definitions (aka test-plans)

        :param data_dir:
            path of the directory with files used by jobs at runtime

        :param bin_dir:
            path of the directory with additional executables

        :param locale_dir:
            path of the directory with locale database (translation catalogs)
        """
        # Meta-data
        self._name = name
        self._version = version
        self._description = description
        self._secure = secure
        self._gettext_domain = gettext_domain
        # Directories
        self._jobs_dir = jobs_dir
        self._whitelists_dir = whitelists_dir
        self._data_dir = data_dir
        self._bin_dir = bin_dir
        self._locale_dir = locale_dir
        # Loaded data
        if self.whitelists_dir is not None:
            whitelists_dir_list = [self.whitelists_dir]
        else:
            whitelists_dir_list = []
        self._whitelist_collection = FsPlugInCollection(
            whitelists_dir_list, ext=".whitelist", wrapper=WhiteListPlugIn)
        if self.jobs_dir is not None:
            jobs_dir_list = [self.jobs_dir]
        else:
            jobs_dir_list = []
        self._job_collection = FsPlugInCollection(
            jobs_dir_list, ext=(".txt", ".txt.in"),
            wrapper=JobDefinitionPlugIn, provider=self)

    @classmethod
    def from_definition(cls, definition, secure):
        """
        Initialize a provider from Provider1Definition object

        :param definition:
            A Provider1Definition object to use as reference

        :param secure:
            Value of the secure flag. This cannot be expressed by a definition
            object.

        This method simplifies initialization of a Provider1 object where the
        caller already has a Provider1Definition object. Depending on the value
        of ``definition.location`` all of the directories are either None or
        initialized to a *good* (typical) value relative to *location*

        The only value that you may want to adjust, for working with source
        providers, is *locale_dir*, by default it would be ``location/locale``
        but ``manage.py i18n`` creates ``location/build/mo``
        """
        # Compute all the directories, depending on the value of
        # definition.location and presence, or lack of thereof, of each
        # directory definition entry.
        jobs_dir = None
        whitelists_dir = None
        data_dir = None
        bin_dir = None
        locale_dir = None
        if definition.location is not Unset:
            # When location is not Unset, all the relevant directories _may_ be
            # subdirectories of location (unless overridden by explicit value).
            # Since all of the directories are validated we take special care
            # not to assign directories that may not exist.
            if definition.jobs_dir is Unset:
                jobs_dir = os.path.join(definition.location, "jobs")
                if not os.path.isdir(jobs_dir):
                    jobs_dir = None
            if definition.whitelists_dir is Unset:
                whitelists_dir = os.path.join(definition.location,
                                              "whitelists")
                if not os.path.isdir(whitelists_dir):
                    whitelists_dir = None
            if definition.data_dir is Unset:
                data_dir = os.path.join(definition.location, "data")
                if not os.path.isdir(data_dir):
                    data_dir = None
            if definition.bin_dir is Unset:
                bin_dir = os.path.join(definition.location, "bin")
                if not os.path.isdir(bin_dir):
                    bin_dir = None
            if definition.locale_dir is Unset:
                locale_dir = os.path.join(definition.location, "locale")
                if not os.path.isdir(locale_dir):
                    locale_dir = None
        else:
            if definition.jobs_dir is not Unset:
                jobs_dir = definition.jobs_dir
            if definition.whitelists_dir is not Unset:
                whitelists_dir = definition.whitelists_dir
            if definition.data_dir is not Unset:
                data_dir = definition.data_dir
            if definition.bin_dir is not Unset:
                bin_dir = definition.bin_dir
            if definition.locale_dir is not Unset:
                locale_dir = definition.locale_dir
        # Get gettext domain
        if definition.gettext_domain is not Unset:
            gettext_domain = definition.gettext_domain
        else:
            gettext_domain = None
        # Initialize the provider object
        return cls(
            definition.name, definition.version, definition.description,
            secure, gettext_domain, jobs_dir, whitelists_dir, data_dir,
            bin_dir, locale_dir)

    def __repr__(self):
        return "<{} name:{!r}>".format(self.__class__.__name__, self.name)

    @property
    def name(self):
        """
        name of this provider
        """
        return self._name

    @property
    def version(self):
        """
        version of this provider
        """
        return self._version

    @property
    def description(self):
        """
        description of this provider
        """
        return self._description

    @property
    def jobs_dir(self):
        """
        absolute path of the jobs directory
        """
        return self._jobs_dir

    @property
    def whitelists_dir(self):
        """
        absolute path of the whitelist directory
        """
        return self._whitelists_dir

    @property
    def data_dir(self):
        """
        absolute path of the data directory
        """
        return self._data_dir

    @property
    def bin_dir(self):
        """
        absolute path of the bin directory

        .. note::
            The programs in that directory may not work without setting
            PYTHONPATH and CHECKBOX_SHARE.
        """
        return self._bin_dir

    @property
    def locale_dir(self):
        """
        absolute path of the directory with locale data

        The value is applicable as argument bindtextdomain()
        """
        return self._locale_dir

    @property
    def CHECKBOX_SHARE(self):
        """
        required value of CHECKBOX_SHARE environment variable.

        .. note::
            This variable is only required by one script.
            It would be nice to remove this later on.
        """
        return os.path.join(self._data_dir, "..")

    @property
    def extra_PYTHONPATH(self):
        """
        additional entry for PYTHONPATH, if needed.

        This entry is required for CheckBox scripts to import the correct
        CheckBox python libraries.

        .. note::
            The result may be None
        """
        return None

    @property
    def secure(self):
        """
        flag indicating that this provider was loaded from the secure portion
        of PROVIDERPATH and thus can be used with the
        plainbox-trusted-launcher-1.
        """
        return self._secure

    @property
    def gettext_domain(self):
        """
        the name of the gettext domain associated with this provider

        This value may be empty, in such case provider data cannot be localized
        for the user environment.
        """
        return self._gettext_domain

    def get_builtin_whitelists(self):
        """
        Load all the whitelists from :attr:`whitelists_dir` and return them

        This method looks at the whitelist directory and loads all files ending
        with .whitelist as a WhiteList object.

        :returns:
            A list of :class:`~plainbox.impl.secure.qualifiers.WhiteList`
            objects sorted by
            :attr:`plainbox.impl.secure.qualifiers.WhiteList.name`.
        :raises IOError, OSError:
            if there were any problems accessing files or directories.  Note
            that OSError is silently ignored when the `whitelists_dir`
            directory is missing.
        """
        self._whitelist_collection.load()
        if self._whitelist_collection.problem_list:
            raise self._whitelist_collection.problem_list[0]
        else:
            return sorted(self._whitelist_collection.get_all_plugin_objects(),
                          key=lambda whitelist: whitelist.name)

    def get_builtin_jobs(self):
        """
        Load and parse all of the job definitions of this provider.

        :returns:
            A sorted list of JobDefinition objects
        :raises RFC822SyntaxError:
            if any of the loaded files was not valid RFC822
        :raises IOError, OSError:
            if there were any problems accessing files or directories.
            Note that OSError is silently ignored when the `jobs_dir`
            directory is missing.

        ..note::
            This method should not be used anymore. Consider transitioning your
            code to :meth:`load_all_jobs()` which is more reliable.
        """
        job_list, problem_list = self.load_all_jobs()
        if problem_list:
            raise problem_list[0]
        else:
            return job_list

    def load_all_jobs(self):
        """
        Load and parse all of the job definitions of this provider.

        Unlike :meth:`get_builtin_jobs()` this method does not stop after the
        first problem encountered and instead collects all of the problems into
        a list which is returned alongside the job list.

        :returns:
            Pair (job_list, problem_list) where each job_list is a sorted list
            of JobDefinition objects and each item from problem_list is an
            exception.
        """
        self._job_collection.load()
        job_list = sorted(
            itertools.chain(
                *self._job_collection.get_all_plugin_objects()),
            key=lambda job: job.id)
        problem_list = self._job_collection.problem_list
        return job_list, problem_list

    def get_all_executables(self):
        """
        Discover and return all executables offered by this provider

        :returns:
            list of executable names (without the full path)
        :raises IOError, OSError:
            if there were any problems accessing files or directories. Note
            that OSError is silently ignored when the `bin_dir` directory is
            missing.
        """
        executable_list = []
        if self.bin_dir is None:
            return executable_list
        try:
            items = os.listdir(self.bin_dir)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                items = []
            else:
                raise
        for name in items:
            filename = os.path.join(self.bin_dir, name)
            if os.access(filename, os.F_OK | os.X_OK):
                executable_list.append(filename)
        return sorted(executable_list)


class IQNValidator(PatternValidator):
    """
    A validator for provider name.

    Provider names use a RFC3720 IQN-like identifiers composed of the follwing
    parts:

    * year
    * (dot separating the next section)
    * domain name
    * (colon separating the next section)
    * identifier

    Each of the fields has an informal definition below:

        year:
            four digit number
        domain name:
            identifiers separated by dots, at least one dot has to be present
        identifier:
            `[a-z][a-z0-9-]*`
    """

    def __init__(self):
        super(IQNValidator, self).__init__(
            "^[0-9]{4}\.[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)+:[a-z][a-z0-9-]*$")

    def __call__(self, variable, new_value):
        if super(IQNValidator, self).__call__(variable, new_value):
            return _("must look like RFC3720 IQN")


class VersionValidator(PatternValidator):
    """
    A validator for provider provider version.

    Provider version must be a sequence of non-negative numbers separated by
    dots. At most one version number must be present, which may be followed by
    any sub-versions.
    """

    def __init__(self):
        super().__init__("^[0-9]+(\.[0-9]+)*$")

    def __call__(self, variable, new_value):
        if super().__call__(variable, new_value):
            return _("must be a sequence of digits separated by dots")


class ExistingDirectoryValidator(IValidator):
    """
    A validator that checks that the value points to an existing directory
    """

    def __call__(self, variable, new_value):
        if not os.path.isdir(new_value):
            return _("no such directory")


class AbsolutePathValidator(IValidator):
    """
    A validator that checks that the value is an absolute path
    """

    def __call__(self, variable, new_value):
        if not os.path.isabs(new_value):
            return _("cannot be relative")


class Provider1Definition(Config):
    """
    A Config-like class for parsing plainbox provider definition files

    .. note::

        The location attribute is special, if set, it defines the base
        directory of *all* the other directory attributes. If location is
        unset, then all the directory attributes default to None (that is,
        there is no directory of that type). This is actually a convention that
        is implemented in :class:`Provider1PlugIn`. Here, all the attributes
        can be Unset and their validators only check values other than Unset.
    """

    location = Variable(
        section='PlainBox Provider',
        help_text=_("Base directory with provider data"),
        validator_list=[
            # NOTE: it *can* be unset!
            NotEmptyValidator(),
            AbsolutePathValidator(),
            ExistingDirectoryValidator(),
        ])

    name = Variable(
        section='PlainBox Provider',
        help_text=_("Name of the provider"),
        validator_list=[
            NotUnsetValidator(),
            NotEmptyValidator(),
            IQNValidator(),
        ])

    @property
    def name_without_colon(self):
        return self.name.replace(':', '.')

    version = Variable(
        section='PlainBox Provider',
        help_text=_("Version of the provider"),
        validator_list=[
            NotUnsetValidator(),
            NotEmptyValidator(),
            VersionValidator(),
        ])

    description = Variable(
        section='PlainBox Provider',
        help_text=_("Description of the provider"))

    gettext_domain = Variable(
        section='PlainBox Provider',
        help_text=_("Name of the gettext domain for translations"),
        validator_list=[
            # NOTE: it *can* be unset!
            PatternValidator("[a-z0-9_-]+"),
        ])

    @property
    def effective_gettext_domain(self):
        """
        effective value of gettext_domian

        The effective value is :meth:`gettex_domain` itself, unless it is
        Unset. If it is Unset the effective value None.
        """
        if self.gettext_domain is not Unset:
            return self.gettext_domain

    jobs_dir = Variable(
        section='PlainBox Provider',
        help_text=_("Pathname of the directory with job definitions"),
        validator_list=[
            # NOTE: it *can* be unset
            NotEmptyValidator(),
            AbsolutePathValidator(),
            ExistingDirectoryValidator(),
        ])

    @property
    def implicit_jobs_dir(self):
        """
        implicit value of jobs_dir (if Unset)

        The implicit value is only defined if location is not Unset. It is the
        'jobs' subdirectory of the directory that location points to.
        """
        if self.location is not Unset:
            return os.path.join(self.location, "jobs")

    @property
    def effective_jobs_dir(self):
        """
        effective value of jobs_dir

        The effective value is :meth:`jobs_dir` itself, unless it is Unset. If
        it is Unset the effective value is the :meth:`implicit_jobs_dir`, if
        that value would be valid. The effective value may be None.
        """
        if self.jobs_dir is not Unset:
            return self.jobs_dir
        implicit = self.implicit_jobs_dir
        if implicit is not None and os.path.isdir(implicit):
            return implicit

    whitelists_dir = Variable(
        section='PlainBox Provider',
        help_text=_("Pathname of the directory with whitelists definitions"),
        validator_list=[
            # NOTE: it *can* be unset
            NotEmptyValidator(),
            AbsolutePathValidator(),
            ExistingDirectoryValidator(),
        ])

    @property
    def implicit_whitelists_dir(self):
        """
        implicit value of whitelists_dir (if Unset)

        The implicit value is only defined if location is not Unset. It is the
        'whitelists' subdirectory of the directory that location points to.
        """
        if self.location is not Unset:
            return os.path.join(self.location, "whitelists")

    @property
    def effective_whitelists_dir(self):
        """
        effective value of whitelists_dir

        The effective value is :meth:`whitelists_dir` itself, unless it is
        Unset. If it is Unset the effective value is the
        :meth:`implicit_whitelists_dir`, if that value would be valid. The
        effective value may be None.
        """
        if self.whitelists_dir is not Unset:
            return self.whitelists_dir
        implicit = self.implicit_whitelists_dir
        if implicit is not None and os.path.isdir(implicit):
            return implicit

    data_dir = Variable(
        section='PlainBox Provider',
        help_text=_("Pathname of the directory with provider data"),
        validator_list=[
            # NOTE: it *can* be unset
            NotEmptyValidator(),
            AbsolutePathValidator(),
            ExistingDirectoryValidator(),
        ])

    @property
    def implicit_data_dir(self):
        """
        implicit value of data_dir (if Unset)

        The implicit value is only defined if location is not Unset. It is the
        'data' subdirectory of the directory that location points to.
        """
        if self.location is not Unset:
            return os.path.join(self.location, "data")

    @property
    def effective_data_dir(self):
        """
        effective value of data_dir

        The effective value is :meth:`data_dir` itself, unless it is Unset. If
        it is Unset the effective value is the :meth:`implicit_data_dir`, if
        that value would be valid. The effective value may be None.
        """
        if self.data_dir is not Unset:
            return self.data_dir
        implicit = self.implicit_data_dir
        if implicit is not None and os.path.isdir(implicit):
            return implicit

    bin_dir = Variable(
        section='PlainBox Provider',
        help_text=_("Pathname of the directory with provider executables"),
        validator_list=[
            # NOTE: it *can* be unset
            NotEmptyValidator(),
            AbsolutePathValidator(),
            ExistingDirectoryValidator(),
        ])

    @property
    def implicit_bin_dir(self):
        """
        implicit value of bin_dir (if Unset)

        The implicit value is only defined if location is not Unset. It is the
        'bin' subdirectory of the directory that location points to.
        """
        if self.location is not Unset:
            return os.path.join(self.location, "bin")

    @property
    def effective_bin_dir(self):
        """
        effective value of bin_dir

        The effective value is :meth:`bin_dir` itself, unless it is Unset. If
        it is Unset the effective value is the :meth:`implicit_bin_dir`, if
        that value would be valid. The effective value may be None.
        """
        if self.bin_dir is not Unset:
            return self.bin_dir
        implicit = self.implicit_bin_dir
        if implicit is not None and os.path.isdir(implicit):
            return implicit

    locale_dir = Variable(
        section='PlainBox Provider',
        help_text=_("Pathname of the directory with locale data"),
        validator_list=[
            # NOTE: it *can* be unset
            NotEmptyValidator(),
            AbsolutePathValidator(),
            ExistingDirectoryValidator(),
        ])

    @property
    def implicit_locale_dir(self):
        """
        implicit value of locale_dir (if Unset)

        The implicit value is only defined if location is not Unset. It is the
        'locale' subdirectory of the directory that location points to.
        """
        if self.location is not Unset:
            return os.path.join(self.location, "locale")

    @property
    def effective_locale_dir(self):
        """
        effective value of locale_dir

        The effective value is :meth:`locale_dir` itself, unless it is Unset.
        If it is Unset the effective value is the :meth:`implicit_locale_dir`,
        if that value would be valid. The effective value may be None.
        """
        if self.locale_dir is not Unset:
            return self.locale_dir
        implicit = self.implicit_locale_dir
        if implicit is not None and os.path.isdir(implicit):
            return implicit


class Provider1PlugIn(IPlugIn):
    """
    A specialized IPlugIn that loads Provider1 instances from their definition
    files
    """

    def __init__(self, filename, definition_text):
        """
        Initialize the plug-in with the specified name and external object
        """
        definition = Provider1Definition()
        # Load the provider definition
        definition.read_string(definition_text)
        # any validation issues prevent plugin from being used
        if definition.problem_list:
            # take the earliest problem and report it
            exc = definition.problem_list[0]
            raise PlugInError(
                _("Problem in provider definition, field {!a}: {}").format(
                    exc.variable.name, exc.message))
        # Get the secure flag
        secure = os.path.dirname(filename) in get_secure_PROVIDERPATH_list()
        # Initialize the provider object
        self._provider = Provider1.from_definition(definition, secure)

    def __repr__(self):
        return "<{!s} plugin_name:{!r}>".format(
            type(self).__name__, self.plugin_name)

    @property
    def plugin_name(self):
        """
        plugin name, the namespace of the provider
        """
        return self._provider.name

    @property
    def plugin_object(self):
        """
        plugin object, the actual Provider1 instance
        """
        return self._provider


def get_secure_PROVIDERPATH_list():
    """
    Computes the secure value of PROVIDERPATH

    This value is used by `plainbox-trusted-launcher-1` executable to discover
    all secure providers.

    :returns:
        A list of two strings:
        * `/usr/local/share/plainbox-providers-1`
        * `/usr/share/plainbox-providers-1`
    """
    return ["/usr/local/share/plainbox-providers-1",
            "/usr/share/plainbox-providers-1"]


class SecureProvider1PlugInCollection(FsPlugInCollection):
    """
    A collection of v1 provider plugins.

    This FsPlugInCollection subclass carries proper, built-in defaults, that
    make loading providers easier.

    This particular class loads providers from the system-wide managed
    locations. This defines the security boundary, as if someone can compromise
    those locations then they already own the corresponding system. In
    consequence this plug in collection does not respect ``PROVIDERPATH``, it
    cannot be customized to load provider definitions from any other location.
    This feature is supported by the
    :class:`plainbox.impl.providers.v1.InsecureProvider1PlugInCollection`
    """

    def __init__(self):
        dir_list = get_secure_PROVIDERPATH_list()
        super().__init__(dir_list, '.provider', wrapper=Provider1PlugIn)


# Collection of all providers
all_providers = SecureProvider1PlugInCollection()
