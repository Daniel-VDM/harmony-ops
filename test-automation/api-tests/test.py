#!/usr/bin/env python3
import argparse
import os
import inspect
import random
import shutil
import time
import sys

import pexpect
import pyhmy

from utils import *

ACC_NAMES_ADDED = []
ACC_NAME_PREFIX = "_Test_key_"


def parse_args():
    parser = argparse.ArgumentParser(description='Wrapper python script to test API using newman.')
    parser.add_argument("--test_dir", dest="test_dir", default="./tests/default",
                        help="Path to test directory. Default is './tests/default'", type=str)
    parser.add_argument("--iterations", dest="iterations", default=5,
                        help="Number of attempts for a successful test. Default is 5.", type=int)
    parser.add_argument("--start_epoch", dest="start_epoch", default=1,
                        help="The minimum epoch before starting tests. Default is 1.", type=int)
    parser.add_argument("--rpc_endpoint_src", dest="endpoint_src", default="https://api.s0.b.hmny.io/",
                        help="Source endpoint for Cx. Default is https://api.s0.b.hmny.io/", type=str)
    parser.add_argument("--rpc_endpoint_dst", dest="endpoint_dst", default="https://api.s1.b.hmny.io/",
                        help="Destination endpoint for Cx. Default is https://api.s1.b.hmny.io/", type=str)
    parser.add_argument("--src_shard", dest="src_shard", default=None, type=str,
                        help=f"The source shard of the Cx. Default assumes associated shard from src endpoint.")
    parser.add_argument("--dst_shard", dest="dst_shard", default=None, type=str,
                        help=f"The destination shard of the Cx. Default assumes associated shard from dst endpoint.")
    parser.add_argument("--exp_endpoint", dest="endpoint_exp", default="http://e0.b.hmny.io:5000/",
                        help="Default is http://e0.b.hmny.io:5000/", type=str)
    parser.add_argument("--delay", dest="txn_delay", default=45,
                        help="The time to wait before checking if a Cx/Tx is on the blockchain. "
                             "Default is 45 seconds. (Input is in seconds)", type=int)
    parser.add_argument("--chain_id", dest="chain_id", default="testnet",
                        help="Chain ID for the CLI. Default is 'testnet'", type=str)
    parser.add_argument("--cli_path", dest="hmy_binary_path", default=None,
                        help=f"ABSOLUTE PATH of CLI binary. "
                             f"Default uses the CLI included in pyhmy module", type=str)
    parser.add_argument("--cli_passphrase", dest="passphrase", default='',
                        help=f"Passphrase used to unlock the keystore. "
                             f"Default is ''", type=str)
    parser.add_argument("--keystore", dest="keys_dir", default="TestnetValidatorKeys",
                        help=f"Directory of keystore to import. Must follow the format of CLI's keystore. "
                             f"Default is ./TestnetValidatorKeys", type=str)
    parser.add_argument("--ignore_regression_test", dest="ignore_regression_test", action='store_true', default=False,
                        help="Disable the regression tests.")
    parser.add_argument("--ignore_staking_test", dest="ignore_staking_test", action='store_true', default=False,
                        help="Disable the staking tests.")
    parser.add_argument("--debug", dest="debug", action='store_true', default=False,
                        help="Enable debug printing.")
    return parser.parse_args()


def get_balance(name, node):
    address = CLI.get_address(name)
    return json_load(CLI.single_call(f"hmy --node={node} balances {address}")) if address else []


def add_key(name):
    proc = CLI.expect_call(f"hmy keys add {name} --use-own-passphrase")
    proc.expect("Enter passphrase:\r\n")
    proc.sendline(f"{args.passphrase}")
    proc.expect("Repeat the passphrase:\r\n")
    proc.sendline(f"{args.passphrase}")
    proc.wait()
    ACC_NAMES_ADDED.append(name)


def get_faucet_account(min_funds):
    """
    Only looks for accounts that have funds on shard 0.
    """
    for acc_name in ACC_NAMES_ADDED:
        if float(get_balance(acc_name, args.endpoint_src)[0]["amount"]) > min_funds:
            return acc_name
    raise RuntimeError(f"None of the loaded accounts have at least {min_funds} on shard 0")


def bls_generator(count, key_dir="/tmp", filter_fn=None):
    assert os.path.isabs(key_dir)
    if not os.path.exists(key_dir):
        os.makedirs(key_dir)
    if filter_fn is not None:
        assert callable(filter_fn)
        assert len(inspect.signature(filter_fn).parameters) == 1, "filter function must have 1 argument"

    for i in range(count):
        while True:
            proc = CLI.expect_call(f"hmy keys generate-bls-key --bls-file-path {key_dir}/{ACC_NAME_PREFIX}bls{i}.key",
                                   timeout=3)
            proc.expect("Enter passphrase:\r\n")
            proc.sendline(f"{args.passphrase}")
            proc.expect("Repeat the passphrase:\r\n")
            proc.sendline(f"{args.passphrase}")
            bls_key = json_load(proc.read().decode().strip())
            if filter_fn is None or filter_fn(bls_key):
                break
        yield bls_key


@announce
def load_keys():
    """
    Makes strong assumption that keyfile ends with '.key' or '--' (for CLI generated keystore files).
    """
    key_paths = os.listdir(args.keys_dir)
    for i, key in enumerate(key_paths):
        if not os.path.isdir(f"{args.keys_dir}/{key}"):
            continue
        key_content = os.listdir(f"{args.keys_dir}/{key}")
        account_name = f"{ACC_NAME_PREFIX}funding_{i}"
        CLI.remove_account(account_name)
        for file_name in key_content:
            if not file_name.endswith(".key") and not file_name.endswith("--"):
                continue
            from_key_file_path = f"{os.path.abspath(args.keys_dir)}/{key}/{file_name}"
            to_key_file_path = f"{CLI.keystore_path}/{account_name}"
            if not os.path.isdir(to_key_file_path):
                os.mkdir(to_key_file_path)
            shutil.copy(from_key_file_path, to_key_file_path)
            ACC_NAMES_ADDED.append(account_name)
    assert len(ACC_NAMES_ADDED) > 1, "Must load at least 2 keys and must match CLI's keystore format"


@announce
def fund_account(from_account_name, to_account_name, amount):
    """
    Assumes from_account has funds on shard 0.
    """
    from_address = CLI.get_address(from_account_name)
    to_address = CLI.get_address(to_account_name)
    CLI.single_call(f"hmy --node={args.endpoint_src} transfer --from {from_address} --to {to_address} "
                    f"--from-shard 0 --to-shard 0 --amount {amount} --chain-id {args.chain_id} "
                    f"--passphrase={args.passphrase} --wait-for-confirm=45", timeout=45)
    if args.debug:
        print(f"{COLOR.OKGREEN}Balances for {to_account_name} ({to_address}):{COLOR.ENDC}")
        print(f"{json.dumps(get_balance(to_account_name, args.endpoint_src), indent=4)}\n")


# TODO: update BLS key logic to match CLI.
@test
def create_simple_validators(validator_count):
    """
    Returns a dictionary of added validators where key = address and value = dictionary
    of associated reference data.

    Note that each staking test assumes that the reference data will be updated (if necessary)
    as it is the single source of truth.
    """
    endpoint = get_endpoint(0, args.endpoint_src)
    amount = 3  # Must be > 1 b/c of min-self-delegation
    faucet_acc_name = get_faucet_account(validator_count * (amount + 1))  # +1/new_acc for gas overhead
    validator_addresses = {}

    for i, bls_key in enumerate(bls_generator(validator_count, key_dir="/tmp/simple_val")):
        val_name = f"{ACC_NAME_PREFIX}validator{i}"
        CLI.remove_account(val_name)
        add_key(val_name)
        val_address = CLI.get_address(val_name)
        fund_account(faucet_acc_name, val_name, amount + 1)  # +1 for gas overhead.
        rates = round(random.uniform(0, 1), 18), round(random.uniform(0, 1), 18)
        rate, max_rate = min(rates), max(rates)
        max_change_rate = round(random.uniform(0, max_rate - 1e-9), 18)
        max_total_delegation = random.randint(amount + 1, 10)  # +1 for delegation.
        proc = CLI.expect_call(f"hmy --node={endpoint} staking create-validator "
                               f"--validator-addr {val_address} --name {val_name} "
                               f"--identity test_account --website harmony.one "
                               f"--security-contact Daniel-VDM --details none --rate {rate} --max-rate {max_rate} "
                               f"--max-change-rate {max_change_rate} --min-self-delegation 1 "
                               f"--max-total-delegation {max_total_delegation} "
                               f"--amount {amount} --bls-pubkeys {bls_key['public-key']} "
                               f"--chain-id {args.chain_id} --passphrase={args.passphrase}")
        pub_key_str = str(bls_key["public-key"])[2:]
        proc.expect(f"For bls public key: {pub_key_str}\r\n")
        proc.expect("Enter the absolute path to the encrypted bls private key file:\r\n")
        proc.sendline(bls_key["encrypted-private-key-path"])
        proc.expect("Enter the bls passphrase:\r\n")
        proc.sendline(f"{args.passphrase}")
        proc.expect(pexpect.EOF)
        txn = json_load(proc.before.decode())
        assert "transaction-receipt" in txn.keys()
        assert txn["transaction-receipt"] is not None
        print(f"{COLOR.OKGREEN}Sent create validator for "
              f"{val_address}:{COLOR.ENDC}\n{json.dumps(txn, indent=4)}\n")
        ref_data = {
            "time-created": datetime.datetime.utcnow().strftime(TIMESTAMP_FORMAT),
            "pub-bls-keys": [bls_key['public-key']],
            "amount": amount,
            "rate": rate,
            "max_rate": max_rate,
            "max_change_rate": max_change_rate,
            "max_total_delegation": max_total_delegation,
            "min_self_delegation": 1,
            "keystore_name": val_name,
        }
        if args.debug:
            print(f"Reference data for {val_address}: {json.dumps(ref_data, indent=4)}")
        validator_addresses[val_address] = ref_data
    return validator_addresses


@test
def create_custom_validators():
    """
    Similar to create_simple_validators except we are using acc keys + BLS keys from main repo.

    Note that this assumes that the keystore of the CLI has the appropriate keys and that the
    given passphrase can unlock it.
    """
    main_repo_accs = [
        ("one1ghkz3frhske7emk79p7v2afmj4a5t0kmjyt4s5",
         "eca09c1808b729ca56f1b5a6a287c6e1c3ae09e29ccf7efa35453471fcab07d9f73cee249e2b91f5ee44eb9618be3904"),
        ("one1d7jfnr6yraxnrycgaemyktkmhmajhp8kl0yahv",
         "f47238daef97d60deedbde5302d05dea5de67608f11f406576e363661f7dcbc4a1385948549b31a6c70f6fde8a391486"),
        ("one1r4zyyjqrulf935a479sgqlpa78kz7zlcg2jfen",
         "fc4b9c535ee91f015efff3f32fbb9d32cdd9bfc8a837bb3eee89b8fff653c7af2050a4e147ebe5c7233dc2d5df06ee0a"),
        ("one1p7ht2d4kl8ve7a8jxw746yfnx4wnfxtp8jqxwe",
         "ca86e551ee42adaaa6477322d7db869d3e203c00d7b86c82ebee629ad79cb6d57b8f3db28336778ec2180e56a8e07296"),
        ("one1z05g55zamqzfw9qs432n33gycdmyvs38xjemyl",
         "95117937cd8c09acd2dfae847d74041a67834ea88662a7cbed1e170350bc329e53db151e5a0ef3e712e35287ae954818"),
        ("one1ljznytjyn269azvszjlcqvpcj6hjm822yrcp2e",
         "68ae289d73332872ec8d04ac256ca0f5453c88ad392730c5741b6055bc3ec3d086ab03637713a29f459177aaa8340615"),
        ("one1uyshu2jgv8w465yc8kkny36thlt2wvel89tcmg",
         "a547a9bf6fdde4f4934cde21473748861a3cc0fe8bbb5e57225a29f483b05b72531f002f8187675743d819c955a86100"),
        ("one103q7qe5t2505lypvltkqtddaef5tzfxwsse4z7",
         "678ec9670899bf6af85b877058bea4fc1301a5a3a376987e826e3ca150b80e3eaadffedad0fedfa111576fa76ded980c"),
        ("one1658znfwf40epvy7e46cqrmzyy54h4n0qa73nep",
         "576d3c48294e00d6be4a22b07b66a870ddee03052fe48a5abbd180222e5d5a1f8946a78d55b025de21635fd743bbad90"),
        ("one1d2rngmem4x2c6zxsjjz29dlah0jzkr0k2n88wc",
         "16513c487a6bb76f37219f3c2927a4f281f9dd3fd6ed2e3a64e500de6545cf391dd973cc228d24f9bd01efe94912e714")
    ]
    endpoint = get_endpoint(0, args.endpoint_src)
    amount = 3  # Must be > 1 b/c of min-self-delegation
    faucet_acc_name = get_faucet_account(len(main_repo_accs) * (amount + 1))  # +1/new_acc for gas overhead
    validator_addresses = {}

    gopath = get_gopath()
    for val_address, key in main_repo_accs:
        val_names = CLI.get_accounts(val_address)
        if not val_names:
            continue
        val_name = list(filter(lambda s: s.startswith(ACC_NAME_PREFIX), val_names))[0]
        if float(get_balance(val_name, endpoint)[0]["amount"]) < amount + 1:
            fund_account(faucet_acc_name, val_name, amount + 1)  # +1 for gas overhead.
        key_path = f"{gopath}/src/github.com/harmony-one/harmony/.hmy/{key}.key"
        assert os.path.isfile(key_path)
        rates = round(random.uniform(0, 1), 18), round(random.uniform(0, 1), 18)
        rate, max_rate = min(rates), max(rates)
        max_change_rate = round(random.uniform(0, max_rate - 1e-9), 18)
        max_total_delegation = random.randint(amount + 1, 100)  # +1 for delegation.
        proc = CLI.expect_call(f"hmy --node={endpoint} staking create-validator "
                               f"--validator-addr {val_address} --name {val_name} "
                               f"--identity test_account --website harmony.one "
                               f"--security-contact Daniel-VDM --details none --rate {rate} --max-rate {max_rate} "
                               f"--max-change-rate {max_change_rate} --min-self-delegation 1 "
                               f"--max-total-delegation {max_total_delegation} "
                               f"--amount {amount} --bls-pubkeys {key} "
                               f"--chain-id {args.chain_id} --passphrase={args.passphrase}")
        proc.expect(f"For bls public key: {key}\r\n")
        proc.expect("Enter the absolute path to the encrypted bls private key file:\r\n")
        proc.sendline(key_path)
        proc.expect("Enter the bls passphrase:\r\n")
        proc.sendline("")  # hardcoded passphrase for these bls keys.
        proc.expect(pexpect.EOF)
        txn = json_load(proc.before.decode())
        assert "transaction-receipt" in txn.keys()
        assert txn["transaction-receipt"] is not None
        print(f"{COLOR.OKGREEN}Sent create validator for "
              f"{val_address}:{COLOR.ENDC}\n{json.dumps(txn, indent=4)}\n")
        ref_data = {
            "time-created": datetime.datetime.utcnow().strftime(TIMESTAMP_FORMAT),
            "pub-bls-keys": [key],
            "amount": amount,
            "rate": rate,
            "max_rate": max_rate,
            "max_change_rate": max_change_rate,
            "max_total_delegation": max_total_delegation,
            "min_self_delegation": 1,
            "keystore_name": val_name,
        }
        if args.debug:
            print(f"Reference data for {val_address}: {json.dumps(ref_data, indent=4)}")
        validator_addresses[val_address] = ref_data
    return validator_addresses


@test
def check_validators(validator_addresses):
    endpoint = get_endpoint(0, args.endpoint_src)
    all_val = json_load(CLI.single_call(f"hmy --node={endpoint} blockchain validator all"))
    assert all_val["result"] is not None
    print(f"{COLOR.OKGREEN}Current validators:{COLOR.ENDC}\n{json.dumps(all_val, indent=4)}\n")
    all_active_val = json_load(CLI.single_call(f"hmy --node={endpoint} blockchain validator all-active"))
    assert all_active_val["result"] is not None
    print(f"{COLOR.OKGREEN}Current ACTIVE validators:{COLOR.ENDC}\n{json.dumps(all_active_val, indent=4)}")

    for address, ref_data in validator_addresses.items():
        print(f"\n{'=' * 85}\n")
        print(f"{COLOR.HEADER}Validator address: {address}{COLOR.ENDC}")
        if address not in all_val["result"]:
            print(f"{COLOR.FAIL}Validator NOT in pool of validators.")
            return False
        else:
            print(f"{COLOR.OKGREEN}Validator in pool of validators.")
        if address not in all_active_val["result"]:
            print(f"{COLOR.WARNING}Validator NOT in pool of ACTIVE validators.")
            # Don't throw an error, just inform.
        else:
            print(f"{COLOR.WARNING}Validator in pool of ACTIVE validators.")
            # Don't throw an error, just inform.
        val_info = json_load(CLI.single_call(f"hmy --node={endpoint} blockchain validator information {address}"))
        print(f"{COLOR.OKGREEN}Validator information:{COLOR.ENDC}\n{json.dumps(val_info, indent=4)}")
        if args.debug:
            print(f"Reference data for {address}: {json.dumps(ref_data, indent=4)}")
        assert val_info["result"] is not None
        reference_keys = set(map(lambda e: int(e, 16), ref_data["pub-bls-keys"]))
        for key in val_info["result"]["slot_pub_keys"]:
            assert int(key, 16) in reference_keys
        assert int(ref_data["max_total_delegation"] * 1e18) == val_info["result"]["max_total_delegation"]
        assert int(ref_data["min_self_delegation"] * 1e18) == val_info["result"]["min_self_delegation"]
        commission_rates = val_info["result"]["commission"]["commission_rates"]
        assert ref_data["rate"] == float(commission_rates["rate"])
        assert ref_data["max_rate"] == float(commission_rates["max_rate"])
        assert ref_data["max_change_rate"] == float(commission_rates["max_change_rate"])
        val_delegation = json_load(CLI.single_call(f"hmy blockchain delegation by-validator {address} "
                                                   f"--node={endpoint}"))
        print(f"{COLOR.OKGREEN}Validator delegation:{COLOR.ENDC}\n{json.dumps(val_delegation, indent=4)}")
        assert val_delegation["result"] is not None
        contains_self_delegation = False
        for delegation in val_delegation["result"]:
            assert delegation["validator_address"] == address
            if delegation["delegator_address"] == address:
                assert not contains_self_delegation, "should not contain duplicate self delegation"
                contains_self_delegation = True
        assert contains_self_delegation
        print(f"\n{'=' * 85}\n")
    return True


@test
def edit_validators(validator_addresses):
    endpoint = get_endpoint(0, args.endpoint_src)
    for (address, ref_data), bls_key in zip(validator_addresses.items(),
                                            bls_generator(len(validator_addresses.keys()), key_dir="/tmp/edit_val")):
        max_total_delegation = ref_data['max_total_delegation'] + random.randint(1, 10)
        old_bls_key = ref_data['pub-bls-keys'].pop()
        proc = CLI.expect_call(f"hmy staking edit-validator --validator-addr {address} "
                               f"--identity test_account --website harmony.one --details none "
                               f"--name {ref_data['keystore_name']} "
                               f"--max-total-delegation {max_total_delegation} "
                               f"--min-self-delegation 1 --rate {ref_data['rate']} --security-contact Leo  "
                               f"--website harmony.one --node={endpoint} "
                               f"--remove-bls-key {old_bls_key}  --add-bls-key {bls_key['public-key']} "
                               f"--chain-id={args.chain_id} --passphrase={args.passphrase}")
        proc.expect("Enter the absolute path to the encrypted bls private key file:\r\n")
        proc.sendline(bls_key["encrypted-private-key-path"])
        proc.expect("Enter the bls passphrase:\r\n")
        proc.sendline(f"{args.passphrase}")
        proc.expect(pexpect.EOF)
        txn = json_load(proc.before.decode())
        assert "transaction-receipt" in txn.keys()
        assert txn["transaction-receipt"] is not None
        print(f"{COLOR.OKGREEN}Sent edit validator for "
              f"{address}:{COLOR.ENDC}\n{json.dumps(txn, indent=4)}\n")
        ref_data["pub-bls-keys"].append(bls_key["public-key"])
        ref_data["max_total_delegation"] = max_total_delegation
    return True


@test
def create_simple_delegators(validator_addresses):
    delegator_addresses = {}
    endpoint = get_endpoint(0, args.endpoint_src)
    for i, (validator_address, data) in enumerate(validator_addresses.items()):
        account_name = f"{ACC_NAME_PREFIX}delegator{i}"
        CLI.remove_account(account_name)
        add_key(account_name)
        delegator_address = CLI.get_address(account_name)
        amount = random.randint(1, data["max_total_delegation"] - data["amount"])
        faucet_acc_name = get_faucet_account(amount + 2)  # 2 for 2x gas overhead.
        fund_account(faucet_acc_name, account_name, amount + 1)  # 1 for gas overhead.
        txn = json_load(CLI.single_call(f"hmy staking delegate --validator-addr {validator_address} "
                                        f"--delegator-addr {delegator_address} --amount {amount} "
                                        f"--node={endpoint} "
                                        f"--chain-id={args.chain_id} --passphrase={args.passphrase}"))
        assert "transaction-receipt" in txn.keys()
        assert txn["transaction-receipt"] is not None
        print(f"{COLOR.OKGREEN}Sent create delegator for "
              f"{delegator_address}:{COLOR.ENDC}\n{json.dumps(txn, indent=4)}\n")
        ref_data = {
            "time-created": datetime.datetime.utcnow().strftime(TIMESTAMP_FORMAT),
            "validator_addresses": [validator_address],
            "amounts": [amount],
            "undelegations": [''],  # This will be a list of strings.
            "keystore_name": account_name
        }
        delegator_addresses[delegator_address] = ref_data
    return delegator_addresses


@test
def check_delegators(delegator_addresses):
    endpoint = get_endpoint(0, args.endpoint_src)
    for address, ref_data in delegator_addresses.items():
        print(f"\n{'=' * 85}\n")
        print(f"{COLOR.HEADER}Delegator address: {address}{COLOR.ENDC}")
        del_delegation = json_load(CLI.single_call(f"hmy blockchain delegation by-delegator {address} "
                                                   f"--node={endpoint}"))
        print(f"{COLOR.OKGREEN}Delegator delegation:{COLOR.ENDC}\n{json.dumps(del_delegation, indent=4)}")
        if args.debug:
            print(f"Reference data for {address}: {json.dumps(ref_data, indent=4)}")
        assert del_delegation["result"] is not None
        assert len(del_delegation["result"]) >= 1
        ref_del_val_addrs = set(ref_data["validator_addresses"])
        for delegation in del_delegation["result"]:
            assert address == delegation["delegator_address"]
            assert delegation["validator_address"] in ref_del_val_addrs
            index = ref_data["validator_addresses"].index(delegation["validator_address"])
            assert delegation["amount"] == int(ref_data["amounts"][index] * 1e18)
            if len(delegation["Undelegations"]) != 0:
                assert json.dumps(delegation["Undelegations"]) == ref_data["undelegations"][index]
        print(f"\n{'=' * 85}\n")
    return True


@test
def undelegate(validator_addresses, delegator_addresses):
    undelegation_epochs = []
    endpoint = get_endpoint(0, args.endpoint_src)
    iterable = zip(validator_addresses.items(), delegator_addresses.items())
    for (v_address, v_ref_data), (d_address, d_ref_data) in iterable:
        assert v_address in d_ref_data["validator_addresses"]
        index = d_ref_data["validator_addresses"].index(v_address)
        amount = d_ref_data["amounts"][index]
        txn = json_load(CLI.single_call(f"hmy staking undelegate --validator-addr {v_address} "
                                        f"--delegator-addr {d_address} --amount {amount} "
                                        f"--node={endpoint} "
                                        f"--chain-id={args.chain_id} --passphrase={args.passphrase}"))
        undelegation_epochs.append(get_current_epoch(endpoint))
        assert "transaction-receipt" in txn.keys()
        assert txn["transaction-receipt"] is not None
        print(f"{COLOR.OKGREEN}Sent undelegate {d_address} from "
              f"{v_address}:{COLOR.ENDC}\n{json.dumps(txn, indent=4)}\n")

    print(f"{COLOR.OKBLUE}Sleeping {args.txn_delay} seconds for finality...{COLOR.ENDC}\n")
    time.sleep(args.txn_delay)

    print(f"{COLOR.OKBLUE}{COLOR.BOLD}Verifying undelegations{COLOR.ENDC}\n")
    iterable = enumerate(zip(validator_addresses.items(), delegator_addresses.items()))
    for i, ((v_address, v_ref_data), (d_address, d_ref_data)) in iterable:
        print(f"\n{'=' * 85}\n")
        print(f"{COLOR.HEADER}Validator address: {v_address}{COLOR.ENDC}")
        print(f"{COLOR.HEADER}Delegator address: {d_address}{COLOR.ENDC}")
        index = d_ref_data["validator_addresses"].index(v_address)
        val_info = json_load(CLI.single_call(f"hmy blockchain delegation by-validator {v_address} "
                                             f"--node={endpoint}"))
        assert val_info["result"] is not None
        print(f"{COLOR.OKGREEN}Validator information:{COLOR.ENDC}\n{json.dumps(val_info, indent=4)}")
        if args.debug:
            print(f"Reference data for (validator) {v_address}: {json.dumps(v_ref_data, indent=4)}")
            print(f"Reference data for (delegator) {d_address}: {json.dumps(d_ref_data, indent=4)}")
        delegator_is_present = False
        for delegation in val_info["result"]:
            if d_address == delegation["delegator_address"]:
                assert not delegator_is_present, "should not see same delegator twice"
                delegator_is_present = True
                assert len(delegation["Undelegations"]) >= 1
                d_ref_data["undelegations"][index] = json.dumps(delegation["Undelegations"])
                undelegation_is_present = False
                for undelegation in delegation["Undelegations"]:
                    if 0 <= abs(undelegation["Epoch"] - undelegation_epochs[i]) <= 1:
                        if undelegation["Epoch"] != undelegation_epochs[i]:
                            print(f"{COLOR.WARNING}WARNING: Undelegation epoch is off by one.{COLOR.ENDC}")
                        assert not undelegation_is_present, "should not see duplicate undelegation"
                        undelegation_is_present = True
                        assert undelegation["Amount"] == int(d_ref_data["amounts"][index] * 1e18)
                assert undelegation_is_present
        assert delegator_is_present
        d_ref_data["amounts"][index] = 0
        print(f"\n{'=' * 85}\n")
    return True


@test
def collect_rewards(address):
    # TODO: put in logic to collect rewards after 7 epochs.
    endpoint = get_endpoint(0, args.endpoint_src)
    staking_command = f"hmy staking collect-rewards --delegator-addr {address} " \
                      f"--node={endpoint} " \
                      f"--chain-id={args.chain_id} --passphrase={args.passphrase}"
    txn = json_load(CLI.single_call(staking_command))
    assert "transaction-receipt" in txn.keys()
    assert txn["transaction-receipt"] is not None
    print(f"{COLOR.OKGREEN}Collection rewards response:{COLOR.ENDC}\n{json.dumps(txn, indent=4)}\n")
    return True


@test
def create_single_validator_many_keys(bls_keys_count):
    """
    Assumes that the CLI asks for the BLS key files in the order of the bls_key_string.
    """
    endpoint = get_endpoint(0, args.endpoint_src)
    amount = 2  # Must be > 1 b/c of min-self-delegation
    faucet_acc_name = get_faucet_account(amount + 5)  # + 5 for gas overheads.
    validator_addresses = {}

    val_name = f"{ACC_NAME_PREFIX}many_keys_validator"
    CLI.remove_account(val_name)
    add_key(val_name)
    fund_account(faucet_acc_name, val_name, amount + 5)
    val_address = CLI.get_address(val_name)
    rates = round(random.uniform(0, 1), 18), round(random.uniform(0, 1), 18)
    rate, max_rate = min(rates), max(rates)
    max_change_rate = round(random.uniform(0, max_rate - 1e-9), 18)
    max_total_delegation = random.randint(amount + 1, 10)
    bls_keys = [d for d in bls_generator(bls_keys_count, key_dir="/tmp/single_val_many_keys")]
    bls_key_string = ','.join(el["public-key"] for el in bls_keys)
    proc = CLI.expect_call(f"hmy --node={endpoint} staking create-validator "
                           f"--validator-addr {val_address} --name {val_name} "
                           f"--identity test_account --website harmony.one "
                           f"--security-contact Daniel-VDM --details none --rate {rate} --max-rate {max_rate} "
                           f"--max-change-rate {max_change_rate} --min-self-delegation 1 "
                           f"--max-total-delegation {max_total_delegation} "
                           f"--amount {amount} --bls-pubkeys {bls_key_string} "
                           f"--chain-id {args.chain_id} --passphrase={args.passphrase}")
    for key in bls_keys:
        pub_key_str = str(key["public-key"])[2:]
        proc.expect(f"For bls public key: {pub_key_str}\r\n")
        proc.expect("Enter the absolute path to the encrypted bls private key file:\r\n")
        proc.sendline(key["encrypted-private-key-path"])
        proc.expect("Enter the bls passphrase:\r\n")
        proc.sendline(f"{args.passphrase}")
    proc.expect(pexpect.EOF)
    txn = json_load(proc.before.decode())
    assert "transaction-receipt" in txn.keys()
    assert txn["transaction-receipt"] is not None
    print(f"{COLOR.OKGREEN}Sent create validator for "
          f"{val_address}:{COLOR.ENDC}\n{json.dumps(txn, indent=4)}\n")
    ref_data = {
        "time-created": datetime.datetime.utcnow().strftime(TIMESTAMP_FORMAT),
        "pub-bls-keys": [key['public-key'] for key in bls_keys],
        "amount": amount,
        "rate": rate,
        "max_rate": max_rate,
        "max_change_rate": max_change_rate,
        "max_total_delegation": max_total_delegation,
        "min_self_delegation": 1,
        "keystore_name": val_name,
    }
    if args.debug:
        print(f"Reference data for {val_address}: {json.dumps(ref_data, indent=4)}")
    validator_addresses[val_address] = ref_data
    return validator_addresses


@announce
def get_raw_cx(passphrase, chain_id, node, src_shard, dst_shard):
    """
    Must be cross shard transaction for tests.
    """
    assert len(ACC_NAMES_ADDED) > 1, "Must load at least 2 keys and must match CLI's keystore format"
    for acc_name in ACC_NAMES_ADDED:
        balances = get_balance(acc_name, node)
        from_addr = CLI.get_address(acc_name)
        to_addr_candidates = ACC_NAMES_ADDED.copy()
        to_addr_candidates.remove(acc_name)
        to_addr = CLI.get_address(random.choice(to_addr_candidates))
        if balances[src_shard]["amount"] >= 5:  # Ensure enough funds (even with high gas fees).
            print(f"Raw transaction details:\n"
                  f"\tNode: {node}\n"
                  f"\tFrom: {from_addr}\n"
                  f"\tTo: {to_addr}\n"
                  f"\tFrom-shard: {src_shard}\n"
                  f"\tTo-shard: {dst_shard}")
            response = CLI.single_call(f"hmy --node={node} transfer --from={from_addr} --to={to_addr} "
                                       f"--from-shard={src_shard} --to-shard={dst_shard} --amount={1e-9} "
                                       f"--chain-id={chain_id} --dry-run --passphrase={passphrase}")
            print(f"\tTransaction for {chain_id}")
            response_lines = response.split("\n")
            assert len(response_lines) == 17, 'CLI output for transaction dry-run is not recognized, check CLI version.'
            transaction = '\n\t\t'.join(response_lines[1:15])
            print(f"\tTransaction:\n\t\t{transaction}")
            return response_lines[-2].replace("RawTxn: ", "")
    raise RuntimeError(f"None of the loaded accounts have funds on shard {src_shard}")


@announce
def setup_newman_no_explorer(test_json, global_json, env_json):
    source_shard = args.src_shard if args.src_shard else get_shard_from_endpoint(args.endpoint_src)
    destination_shard = args.dst_shard if args.dst_shard else get_shard_from_endpoint(args.endpoint_dst)
    raw_txn = get_raw_cx(passphrase=args.passphrase, chain_id=args.chain_id,
                         node=args.endpoint_src, src_shard=source_shard, dst_shard=destination_shard)

    if str(source_shard) not in args.endpoint_src:
        print(f"Source shard {source_shard} may not match source endpoint {args.endpoint_src}")
    if str(destination_shard) not in args.endpoint_dst:
        print(f"Destination shard {destination_shard} may not match destination endpoint {args.endpoint_dst}")

    for i, var in enumerate(env_json["values"]):
        if var["key"] == "rawTransaction":
            env_json["values"][i]["value"] = raw_txn
        if var["key"] == "txn_delay":
            env_json["values"][i]["value"] = args.txn_delay

    for i, var in enumerate(global_json["values"]):
        if var["key"] == "endpoint_src":
            global_json["values"][i]["value"] = args.endpoint_src
        if var["key"] == "endpoint_dst":
            global_json["values"][i]["value"] = args.endpoint_dst


@announce
def setup_newman_only_explorer(test_json, global_json, env_json):
    if "localhost" in args.endpoint_src or "localhost" in args.endpoint_exp:
        print("\n\t[WARNING] This test is for testnet or mainnet.\n")

    source_shard = args.src_shard if args.src_shard else get_shard_from_endpoint(args.endpoint_src)
    destination_shard = args.dst_shard if args.dst_shard else get_shard_from_endpoint(args.endpoint_dst)
    raw_txn = get_raw_cx(passphrase=args.passphrase, chain_id=args.chain_id,
                         node=args.endpoint_src, src_shard=source_shard, dst_shard=destination_shard)

    if str(source_shard) not in args.endpoint_src:
        print(f"Source shard {source_shard} may not match source endpoint {args.endpoint_src}")
    if str(destination_shard) not in args.endpoint_dst:
        print(f"Destination shard {destination_shard} may not match destination endpoint {args.endpoint_dst}")

    for i, var in enumerate(env_json["values"]):
        if var["key"] == "rawTransaction":
            env_json["values"][i]["value"] = raw_txn
        if var["key"] == "tx_beta_endpoint":
            env_json["values"][i]["value"] = args.endpoint_exp
        if var["key"] == "txn_delay":
            env_json["values"][i]["value"] = args.txn_delay
        if var["key"] == "source_shard":
            env_json["values"][i]["value"] = source_shard

    for i, var in enumerate(global_json["values"]):
        if var["key"] == "endpoint_exp":
            global_json["values"][i]["value"] = args.endpoint_exp
        if var["key"] == "endpoint_src":
            global_json["values"][i]["value"] = args.endpoint_src


@announce
def setup_newman_default(test_json, global_json, env_json):
    if "localhost" in args.endpoint_src or "localhost" in args.endpoint_exp:
        print("\n\t[WARNING] This test is for testnet or mainnet.\n")

    source_shard = args.src_shard if args.src_shard else get_shard_from_endpoint(args.endpoint_src)
    destination_shard = args.dst_shard if args.dst_shard else get_shard_from_endpoint(args.endpoint_dst)
    raw_txn = get_raw_cx(passphrase=args.passphrase, chain_id=args.chain_id,
                         node=args.endpoint_src, src_shard=source_shard, dst_shard=destination_shard)

    if str(source_shard) not in args.endpoint_src:
        print(f"Source shard {source_shard} may not match source endpoint {args.endpoint_src}")
    if str(destination_shard) not in args.endpoint_dst:
        print(f"Destination shard {destination_shard} may not match destination endpoint {args.endpoint_dst}")

    for i, var in enumerate(env_json["values"]):
        if var["key"] == "rawTransaction":
            env_json["values"][i]["value"] = raw_txn
        if var["key"] == "tx_beta_endpoint":
            env_json["values"][i]["value"] = args.endpoint_exp
        if var["key"] == "txn_delay":
            env_json["values"][i]["value"] = args.txn_delay
        if var["key"] == "source_shard":
            env_json["values"][i]["value"] = source_shard

    for i, var in enumerate(global_json["values"]):
        if var["key"] == "endpoint_src":
            global_json["values"][i]["value"] = args.endpoint_src
        if var["key"] == "endpoint_dst":
            global_json["values"][i]["value"] = args.endpoint_dst
        if var["key"] == "endpoint_exp":
            global_json["values"][i]["value"] = args.endpoint_exp


def staking_integration_test():
    print(f"{COLOR.UNDERLINE}{COLOR.BOLD} == Running staking integration test == {COLOR.ENDC}")
    test_validators = create_simple_validators(validator_count=1)

    print(f"{COLOR.OKBLUE}Sleeping {args.txn_delay} seconds for finality...")
    time.sleep(args.txn_delay)

    check_validators(test_validators)
    test_delegators = create_simple_delegators(test_validators)

    print(f"{COLOR.OKBLUE}Sleeping {args.txn_delay} seconds for finality...")
    time.sleep(args.txn_delay)

    check_delegators(test_delegators)
    edit_validators(test_validators)

    print(f"{COLOR.OKBLUE}Sleeping {args.txn_delay} seconds for finality...")
    time.sleep(args.txn_delay)

    check_validators(test_validators)
    undelegate(test_validators, test_delegators)
    check_delegators(test_delegators)

    # TODO: Check if the bottom code will break Devnet via localnet test.
    # many_keys_validator_singleton = create_single_validator_many_keys(bls_keys_count=5)
    #
    # print(f"{COLOR.OKBLUE}Sleeping {args.txn_delay} seconds for finality...")
    # time.sleep(args.txn_delay)
    #
    # check_validators(many_keys_validator_singleton)
    # edit_validators(many_keys_validator_singleton)
    #
    # print(f"{COLOR.OKBLUE}Sleeping {args.txn_delay} seconds for finality...")
    # time.sleep(args.txn_delay)
    #
    # check_validators(many_keys_validator_singleton)

    # print(f"{COLOR.OKBLUE}Sleeping {args.txn_delay} seconds for finality...")
    # time.sleep(args.txn_delay)
    # collect_rewards(test_delegators)  # TODO: implement logic for separate trigger.
    return 0  # TODO setup logic to return correct exit code.


def regression_test():
    print(f"{COLOR.UNDERLINE}{COLOR.BOLD} == Running regression test == {COLOR.ENDC}")
    with open(f"{args.test_dir}/test.json", 'r') as f:
        test_json = json.load(f)
    with open(f"{args.test_dir}/global.json", 'r') as f:
        global_json = json.load(f)
    with open(f"{args.test_dir}/env.json", 'r') as f:
        env_json = json.load(f)

    if "Harmony API Tests - no-explorer" in test_json["info"]["name"]:
        setup_newman_no_explorer(test_json, global_json, env_json)
    elif "Harmony API Tests - only-explorer" in test_json["info"]["name"]:
        setup_newman_only_explorer(test_json, global_json, env_json)
    else:
        setup_newman_default(test_json, global_json, env_json)

    with open(f"{args.test_dir}/global.json", 'w') as f:
        json.dump(global_json, f)
    with open(f"{args.test_dir}/env.json", 'w') as f:
        json.dump(env_json, f)

    return_code = 0
    for n in range(args.iterations):
        print(f"\n\tIteration {n+1} out of {args.iterations}\n")
        proc = subprocess.Popen(["newman", "run", f"{args.test_dir}/test.json",
                                 "-e", f"{args.test_dir}/env.json",
                                 "-g", f"{args.test_dir}/global.json"])
        proc.wait()
        return_code = proc.returncode
        if proc.returncode == 0:
            print(f"\n\tSucceeded in {n+1} attempt(s)\n")
            break
    return return_code


if __name__ == "__main__":
    args = parse_args()
    CLI = pyhmy.HmyCLI(environment=pyhmy.get_environment(), hmy_binary_path=args.hmy_binary_path)
    print(f"CLI Version: {CLI.version}")
    assert os.path.isfile(CLI.hmy_binary_path), "CLI binary is not found, specify it with option."
    version_str = re.search('version v.*-', CLI.version).group(0).split('-')[0].replace("version v", "")
    assert int(version_str) >= 170, "CLI binary is the wrong version."
    assert os.path.isdir(args.keys_dir), "Could not find keystore directory"
    assert is_active_shard(args.endpoint_src), "The source shard endpoint is NOT active."
    # assert is_active_shard(args.endpoint_dst), "The destination shard endpoint is NOT active."
    if args.chain_id not in json_load(CLI.single_call("hmy blockchain known-chains")):
        args.chain_id = "testnet"
    exit_code = 0

    try:
        load_keys()

        print(f"Waiting for epoch {args.start_epoch} (or later)")
        while not is_after_epoch(args.start_epoch - 1, args.endpoint_src):
            time.sleep(5)

        if not args.ignore_staking_test:
            code = staking_integration_test()
            if exit_code == 0:
                exit_code = code

        if not args.ignore_regression_test:
            code = regression_test()
            if exit_code == 0:
                exit_code = code

    except (RuntimeError, KeyboardInterrupt) as err:
        print("Removing imported keys from CLI's keystore...")
        for acc in ACC_NAMES_ADDED:
            CLI.remove_account(acc)
        raise err

    print("Removing imported keys from CLI's keystore...")
    for acc in ACC_NAMES_ADDED:
        CLI.remove_account(acc)
    sys.exit(exit_code)
