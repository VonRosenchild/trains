from __future__ import absolute_import, division, print_function

import collections
import re
import threading

import requests
import six
if six.PY3:
    from math import inf
else:
    inf = float('inf')


class InvalidVersion(ValueError):
    """
    An invalid version was found, users should refer to PEP 440.
    """


_Version = collections.namedtuple(
    "_Version", ["epoch", "release", "dev", "pre", "post", "local"]
)


class _BaseVersion(object):
    def __hash__(self):
        return hash(self._key)

    def __lt__(self, other):
        return self._compare(other, lambda s, o: s < o)

    def __le__(self, other):
        return self._compare(other, lambda s, o: s <= o)

    def __eq__(self, other):
        return self._compare(other, lambda s, o: s == o)

    def __ge__(self, other):
        return self._compare(other, lambda s, o: s >= o)

    def __gt__(self, other):
        return self._compare(other, lambda s, o: s > o)

    def __ne__(self, other):
        return self._compare(other, lambda s, o: s != o)

    def _compare(self, other, method):
        if not isinstance(other, _BaseVersion):
            return NotImplemented

        return method(self._key, other._key)


class Version(_BaseVersion):
    VERSION_PATTERN = r"""
        v?
        (?:
            (?:(?P<epoch>[0-9]+)!)?                           # epoch
            (?P<release>[0-9]+(?:\.[0-9]+)*)                  # release segment
            (?P<pre>                                          # pre-release
                [-_\.]?
                (?P<pre_l>(a|b|c|rc|alpha|beta|pre|preview))
                [-_\.]?
                (?P<pre_n>[0-9]+)?
            )?
            (?P<post>                                         # post release
                (?:-(?P<post_n1>[0-9]+))
                |
                (?:
                    [-_\.]?
                    (?P<post_l>post|rev|r)
                    [-_\.]?
                    (?P<post_n2>[0-9]+)?
                )
            )?
            (?P<dev>                                          # dev release
                [-_\.]?
                (?P<dev_l>dev)
                [-_\.]?
                (?P<dev_n>[0-9]+)?
            )?
        )
        (?:\+(?P<local>[a-z0-9]+(?:[-_\.][a-z0-9]+)*))?       # local version
    """
    _regex = re.compile(r"^\s*" + VERSION_PATTERN + r"\s*$", re.VERBOSE | re.IGNORECASE)
    _local_version_separators = re.compile(r"[\._-]")

    def __init__(self, version):
        # Validate the version and parse it into pieces
        match = self._regex.search(version)
        if not match:
            raise InvalidVersion("Invalid version: '{0}'".format(version))

        # Store the parsed out pieces of the version
        self._version = _Version(
            epoch=int(match.group("epoch")) if match.group("epoch") else 0,
            release=tuple(int(i) for i in match.group("release").split(".")),
            pre=self._parse_letter_version(match.group("pre_l"), match.group("pre_n")),
            post=self._parse_letter_version(
                match.group("post_l") or '', match.group("post_n1") or match.group("post_n2") or ''
            ),
            dev=self._parse_letter_version(match.group("dev_l") or '', match.group("dev_n") or ''),
            local=self._parse_local_version(match.group("local") or ''),
        )

        # Generate a key which will be used for sorting
        self._key = self._cmpkey(
            self._version.epoch,
            self._version.release,
            self._version.pre,
            self._version.post,
            self._version.dev,
            self._version.local,
        )

    def __repr__(self):
        return "<Version({0})>".format(repr(str(self)))

    def __str__(self):
        parts = []

        # Epoch
        if self.epoch != 0:
            parts.append("{0}!".format(self.epoch))

        # Release segment
        parts.append(".".join(str(x) for x in self.release))

        # Pre-release
        if self.pre is not None:
            parts.append("".join(str(x) for x in self.pre))

        # Post-release
        if self.post is not None:
            parts.append(".post{0}".format(self.post))

        # Development release
        if self.dev is not None:
            parts.append(".dev{0}".format(self.dev))

        # Local version segment
        if self.local is not None:
            parts.append("+{0}".format(self.local))

        return "".join(parts)

    @property
    def epoch(self):
        return self._version.epoch

    @property
    def release(self):
        return self._version.release

    @property
    def pre(self):
        return self._version.pre

    @property
    def post(self):
        return self._version.post[1] if self._version.post else None

    @property
    def dev(self):
        return self._version.dev[1] if self._version.dev else None

    @property
    def local(self):
        if self._version.local:
            return ".".join(str(x) for x in self._version.local)
        else:
            return None

    @property
    def public(self):
        return str(self).split("+", 1)[0]

    @property
    def base_version(self):
        parts = []

        # Epoch
        if self.epoch != 0:
            parts.append("{0}!".format(self.epoch))

        # Release segment
        parts.append(".".join(str(x) for x in self.release))

        return "".join(parts)

    @property
    def is_prerelease(self):
        return self.dev is not None or self.pre is not None

    @property
    def is_postrelease(self):
        return self.post is not None

    @property
    def is_devrelease(self):
        return self.dev is not None

    @staticmethod
    def _parse_letter_version(letter, number):
        if not letter and not number:
            return None
        if letter:
            # We consider there to be an implicit 0 in a pre-release if there is
            # not a numeral associated with it.
            if number is None:
                number = 0

            # We normalize any letters to their lower case form
            letter = letter.lower()

            # We consider some words to be alternate spellings of other words and
            # in those cases we want to normalize the spellings to our preferred
            # spelling.
            if letter == "alpha":
                letter = "a"
            elif letter == "beta":
                letter = "b"
            elif letter in ["c", "pre", "preview"]:
                letter = "rc"
            elif letter in ["rev", "r"]:
                letter = "post"

            return letter, int(number)
        if not letter and number:
            # We assume if we are given a number, but we are not given a letter
            # then this is using the implicit post release syntax (e.g. 1.0-1)
            letter = "post"

        return letter, int(number)

    @classmethod
    def _parse_local_version(cls, local):
        """
        Takes a string like abc.1.twelve and turns it into ("abc", 1, "twelve").
        """
        if local is not None:
            local = tuple(
                part.lower() if not part.isdigit() else int(part)
                for part in cls._local_version_separators.split(local)
            )
            if not local or not local[0]:
                return None
            return local
        return None

    @staticmethod
    def _cmpkey(epoch, release, pre, post, dev, local):
        # When we compare a release version, we want to compare it with all of the
        # trailing zeros removed. So we'll use a reverse the list, drop all the now
        # leading zeros until we come to something non zero, then take the rest
        # re-reverse it back into the correct order and make it a tuple and use
        # that for our sorting key.
        # release = tuple(
        #     reversed(list(itertools.dropwhile(lambda x: x == 0, reversed(release))))
        # )

        # Versions without a pre-release (except as noted above) should sort after
        # those with one.
        if not pre:
            pre = inf
        elif pre:
            pre = pre[1]

        # Versions without a post segment should sort before those with one.
        if not post:
            post = -inf
        else:
            post = post[1]

        # Versions without a development segment should sort after those with one.
        if not dev:
            dev = inf
        else:
            dev = dev[1]

        if not local:
            # Versions without a local segment should sort before those with one.
            local = inf
        else:
            # Versions with a local segment need that segment parsed to implement
            # the sorting rules in PEP440.
            # - Alpha numeric segments sort before numeric segments
            # - Alpha numeric segments sort lexicographically
            # - Numeric segments sort numerically
            # - Shorter versions sort before longer versions when the prefixes
            #   match exactly
            local = local[1]

        return epoch, release, pre, post, dev, local


class CheckPackageUpdates(object):
    _package_version_checked = False

    @classmethod
    def check_new_package_available(cls, only_once=False):
        """
        :return: true if there is a newer package in PyPI
        """
        if only_once and cls._package_version_checked:
            return None

        # noinspection PyBroadException
        try:
            cls._package_version_checked = True
            # Sending the request only for statistics
            update_statistics = threading.Thread(target=CheckPackageUpdates.get_version_from_updates_server)
            update_statistics.daemon = True
            update_statistics.start()

            releases = requests.get('https://pypi.python.org/pypi/trains/json', timeout=3.0).json()['releases'].keys()

            releases = [Version(r) for r in releases]
            latest_version = sorted(releases)
            from ..version import __version__
            cur_version = Version(__version__)
            if not cur_version.is_devrelease and not cur_version.is_prerelease:
                latest_version = [r for r in latest_version if not r.is_devrelease and not r.is_prerelease]

            if cur_version >= latest_version[-1]:
                return None
            not_patch_upgrade = latest_version[-1].release[:2] != cur_version.release[:2]
            return str(latest_version[-1]), not_patch_upgrade
        except Exception:
            return None

    @staticmethod
    def get_version_from_updates_server():
        try:
            _ = requests.get('https://updates.trainsai.io/updates', timeout=1.0)
        except Exception:
            pass
