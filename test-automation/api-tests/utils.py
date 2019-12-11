import json
import datetime
import re
import subprocess
import traceback
import sys

import requests

TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S %z %Z'


class COLOR:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def get_sharding_structure(endpoint):
    payload = """{
           "jsonrpc": "2.0",
           "method": "hmy_getShardingStructure",
           "params": [  ],
           "id": 1
       }"""
    headers = {
        'Content-Type': 'application/json'
    }
    response = requests.request('POST', endpoint, headers=headers, data=payload, allow_redirects=False, timeout=3)
    return json.loads(response.content)


def is_active_shard(endpoint, delay_tolerance=60):
    """
    :param endpoint: The endpoint of the SHARD to check
    :param delay_tolerance: The time (in seconds) that the shard timestamp can be behind
    :return: If shard is active or not
    """
    payload = """{
            "jsonrpc": "2.0",
            "method": "hmy_latestHeader",
            "params": [  ],
            "id": 1
        }"""
    headers = {
        'Content-Type': 'application/json'
    }
    try:
        curr_time = datetime.datetime.utcnow()
        response = requests.request('POST', endpoint, headers=headers, data=payload, allow_redirects=False, timeout=3)
        body = json.loads(response.content)
        timestamp = datetime.datetime.strptime(body["result"]["timestamp"], TIMESTAMP_FORMAT).replace(tzinfo=None)
        time_delta = curr_time - timestamp
        return abs(time_delta.seconds) < delay_tolerance
    except (requests.ConnectionError, json.decoder.JSONDecodeError, KeyError):
        return False


def is_after_epoch(n, endpoint):
    """
    :param n: The epoch number
    :param endpoint: The endpoint of the SHARD to check
    :return: If it is (strictly) after epoch N
    """
    payload = """{
        "jsonrpc": "2.0",
        "method": "hmy_latestHeader",
        "params": [  ],
        "id": 1
    }"""
    headers = {
        'Content-Type': 'application/json'
    }
    try:
        response = requests.request('POST', endpoint, headers=headers, data=payload, allow_redirects=False, timeout=3)
        body = json.loads(response.content)
        return int(body["result"]["epoch"]) > n
    except (requests.ConnectionError, json.decoder.JSONDecodeError, KeyError):
        return False


def get_shard_from_endpoint(endpoint):
    """
    Currently assumes <= 10 shards
    """
    re_match = re.search('\.s.\.', endpoint)
    if re_match:
        return int(re_match.group(0)[-2])
    re_match = re.search(':950./', endpoint)
    if re_match:
        return int(re_match.group(0)[-2])
    raise ValueError(f"Unknown endpoint format: {endpoint}")


def json_load(string):
    try:
        return json.loads(string)
    except Exception as e:
        print(f"{COLOR.FAIL}Could not parse input: '{string}'{COLOR.ENDC}")
        raise e from e


def get_gopath():
    return subprocess.check_output(["go", "env", "GOPATH"]).decode().strip()


def announce(fn):
    """
    Simple decorator to announce (via printing) that a function has been called.
    """

    def wrap(*args):
        print(f"{COLOR.OKBLUE}{COLOR.BOLD}Running: {fn.__name__}{args}{COLOR.ENDC}")
        return fn(*args)

    return wrap


def test(fn):
    """
    Test function wrapper.
    :return If the test passed or not.
    """

    def wrap(*args):
        print(f"\n\t{COLOR.HEADER}== Start test: {fn.__name__} =={COLOR.ENDC}\n")
        try:
            to_be_returned = fn(*args)
            if to_be_returned:
                print(f"\n\t{COLOR.HEADER}{COLOR.UNDERLINE}== Passed test: {fn.__name__} =={COLOR.ENDC}\n")
            else:
                print(f"\n\t{COLOR.FAIL}{COLOR.UNDERLINE}== FAILED test: {fn.__name__} =={COLOR.ENDC}\n")
            return to_be_returned
        except Exception as e:  # Catch all to continue to other tests in same script.
            print(f"\n\t{COLOR.FAIL}{COLOR.UNDERLINE}== FAILED test: {fn.__name__} =={COLOR.ENDC}\n")
            print(f"{COLOR.FAIL}Exception:\n {e}{COLOR.ENDC}")

    return wrap
