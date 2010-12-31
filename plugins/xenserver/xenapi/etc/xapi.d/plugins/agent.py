#!/usr/bin/env python

# Copyright (c) 2010 Citrix Systems, Inc.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
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

#
# XenAPI plugin for reading/writing information to xenstore
#

try:
    import json
except ImportError:
    import simplejson as json
import os
import random
import subprocess
import tempfile
import time

import XenAPIPlugin

from pluginlib_nova import *
configure_logging("xenstore")
import xenstore

AGENT_TIMEOUT = 30
# Used for simulating an external agent for testing
PRETEND_SECRET = 11111


class TimeoutError(StandardError):
    pass

class SimpleDH(object):
    """This class wraps all the functionality needed to implement
    basic Diffie-Hellman-Merkle key exchange in Python. It features
    intelligent defaults for the prime and base numbers needed for the
    calculation, while allowing you to supply your own. It requires that
    the openssl binary be installed on the system on which this is run,
    as it uses that to handle the encryption and decryption. If openssl
    is not available, a RuntimeError will be raised.

    Please note that nova already uses the M2Crypto library for most
    cryptographic functions, and that it includes a Diffie-Hellman
    implementation. However, that is a much more complex implementation,
    and is not compatible with the DH algorithm that the agent uses. Hence
    the need for this 'simple' version.
    """
    def __init__(self, prime=None, base=None, secret=None):
        """You can specify the values for prime and base if you wish;
        otherwise, reasonable default values will be used.
        """
        if prime is None:
            self._prime = 162259276829213363391578010288127
        else:
            self._prime = prime
        if base is None:
            self._base = 5
        else:
            self._base = base
        if secret is None:
            self._secret = random.randint(5000, 15000)
        else:
            self._secret = secret
        self._shared = self._public = None

    def get_public(self):
        """Return the public key"""
        self._public = (self._base ** self._secret) % self._prime
        return self._public

    def compute_shared(self, other):
        """Given the other end's public key, compute the
        shared secret.
        """
        self._shared = (other ** self._secret) % self._prime
        return self._shared

    def _run_ssl(self, text, which):
        """The encryption/decryption methods require running the openssl
        installed on the system. This method abstracts out the common
        code required.
        """
        base_cmd = ("cat %(tmpfile)s | openssl enc -aes-128-cbc "
                "-a -pass pass:%(shared)s -nosalt %(dec_flag)s")
        if which.lower()[0] == "d":
            dec_flag = " -d"
        else:
            dec_flag = ""
        # Note: instead of using 'cat' and a tempfile, it is also
        # possible to just 'echo' the value. However, we can not assume
        # that the value is 'safe'; i.e., it may contain semi-colons,
        # octothorpes, or other characters that would not be allowed
        # in an 'echo' construct.
        fd, tmpfile = tempfile.mkstemp()
        os.close(fd)
        file(tmpfile, "w").write(text)
        shared = self._shared
        cmd = base_cmd % locals()
        try:
            return _run_command(cmd)
        except PluginError, e:
            raise RuntimeError("OpenSSL error: %s" % e)

    def encrypt(self, text):
        """Uses the shared key to encrypt the given text."""
        return self._run_ssl(text, "enc")

    def decrypt(self, text):
        """Uses the shared key to decrypt the given text."""
        return self._run_ssl(text, "dec")


def _run_command(cmd):
    """Abstracts out the basics of issuing system commands. If the command
    returns anything in stderr, a PluginError is raised with that information.
    Otherwise, the output from stdout is returned.
    """
    pipe = subprocess.PIPE
    proc = subprocess.Popen([cmd], shell=True, stdin=pipe, stdout=pipe, stderr=pipe, close_fds=True)
    proc.wait()
    err = proc.stderr.read()
    if err:
        raise PluginError(err)
    return proc.stdout.read()

def key_init(self, arg_dict):
    """Handles the Diffie-Hellman key exchange with the agent to
    establish the shared secret key used to encrypt/decrypt sensitive
    info to be passed, such as passwords. Returns the shared
    secret key value.
    """
    pub = int(arg_dict["pub"])
    arg_dict["value"] = json.dumps({"name": "keyinit", "value": pub})
    request_id = arg_dict["id"]
    if arg_dict.get("testing_mode"):
        # Pretend!
        pretend = SimpleDH(secret=PRETEND_SECRET)
        shared = pretend.compute_shared(pub)
        # Simulate the agent's response
        ret = '{ "returncode": "D0", "message": "%s", "shared": "%s" }' % (pretend.get_public(), shared)
        return ret
    arg_dict["path"] = "data/host/%s" % request_id
    xenstore.write_record(self, arg_dict)
    try:
        resp = _wait_for_agent(self, request_id, arg_dict)
    except TimeoutError, e:
        raise PluginError("%s" % e)
    return resp

def password(self, arg_dict):
    """Writes a request to xenstore that tells the agent to set
    the root password for the given VM. The password should be 
    encrypted using the shared secret key that was returned by a 
    previous call to key_init. The encrypted password value should
    be passed as the value for the 'enc_pass' key in arg_dict.
    """
    pub = int(arg_dict["pub"])
    enc_pass = arg_dict["enc_pass"]
    if arg_dict.get("testing_mode"):
        # Decrypt the password, and send it back to verify
        pretend = SimpleDH(secret=PRETEND_SECRET)
        pretend.compute_shared(pub)
        pw = pretend.decrypt(enc_pass)
        ret = '{ "returncode": "0", "message": "%s" }' % pw
        return ret
    arg_dict["value"] = json.dumps({"name": "password", "value": enc_pass})
    request_id = arg_dict["id"]
    arg_dict["path"] = "data/host/%s" % request_id
    xenstore.write_record(self, arg_dict)
    try:
        resp = _wait_for_agent(self, request_id, arg_dict)
    except TimeoutError, e:
        raise PluginError("%s" % e)
    return resp

def _wait_for_agent(self, request_id, arg_dict):
    """Periodically checks xenstore for a response from the agent.
    The request is always written to 'data/host/{id}', and
    the agent's response for that request will be in 'data/guest/{id}'.
    If no value appears from the agent within the time specified by
    AGENT_TIMEOUT, the original request is deleted and a TimeoutError
    is returned.
    """
    arg_dict["path"] = "data/guest/%s" % request_id
    arg_dict["ignore_missing_path"] = True
    start = time.time()
    while True:
        if time.time() - start > AGENT_TIMEOUT:
            # No response within the timeout period; bail out
            # First, delete the request record
            arg_dict["path"] = "data/host/%s" % request_id
            xenstore.delete_record(self, arg_dict)
            raise TimeoutError("No response from agent within %s seconds." %
                    AGENT_TIMEOUT)
        ret = xenstore.read_record(self, arg_dict)
        if ret != "None":
            # The agent responded
            return ret
        else:
            time.sleep(3)


if __name__ == "__main__":
    XenAPIPlugin.dispatch(
        {"key_init": key_init,
        "password": password})
