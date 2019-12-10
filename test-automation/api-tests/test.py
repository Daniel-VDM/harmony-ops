#!/usr/bin/env python3
import argparse
import os
import inspect
import random
import shutil
import sys
import time

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
    parser.add_argument("--rpc_endpoint_src", dest="hmy_endpoint_src", default="https://api.s0.b.hmny.io/",
                        help="Source endpoint for Cx. Default is https://api.s0.b.hmny.io/", type=str)
    parser.add_argument("--rpc_endpoint_dst", dest="hmy_endpoint_dst", default="https://api.s1.b.hmny.io/",
                        help="Destination endpoint for Cx. Default is https://api.s1.b.hmny.io/", type=str)
    parser.add_argument("--src_shard", dest="src_shard", default=None, type=str,
                        help=f"The source shard of the Cx. Default assumes associated shard from src endpoint.")
    parser.add_argument("--dst_shard", dest="dst_shard", default=None, type=str,
                        help=f"The destination shard of the Cx. Default assumes associated shard from dst endpoint.")
    parser.add_argument("--exp_endpoint", dest="hmy_exp_endpoint", default="http://e0.b.hmny.io:5000/",
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
    return parser.parse_args()


def get_balance(name, node):
    address = CLI.get_address(name)
    return json.loads(CLI.single_call(f"hmy --node={node} balances {address}")) if address else []


def add_key(name):
    proc = CLI.expect_call(f"hmy keys add {name} --use-own-passphrase")
    proc.expect("Enter passphrase:\r\n")
    proc.sendline(f"{args.passphrase}")
    proc.expect("Repeat the passphrase:\r\n")
    proc.sendline(f"{args.passphrase}")
    proc.wait()
    ACC_NAMES_ADDED.append(name)


def bls_generator(count, key_dir="/tmp", filter_fn=None):
    assert os.path.isabs(key_dir)
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
            bls_key = json.loads(proc.read().decode().strip())
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
    Assumes from_account has been imported with default passphrase.
    """
    from_address = CLI.get_address(from_account_name)
    to_address = CLI.get_address(to_account_name)
    CLI.single_call(f"hmy --node={args.hmy_endpoint_src} transfer --from {from_address} --to {to_address} "
                    f"--from-shard 0 --to-shard 0 --amount {amount} --chain-id {args.chain_id} "
                    f"--passphrase={args.passphrase} --wait-for-confirm=45", timeout=40)
    print(f"{COLOR.OKGREEN}Balances for {to_account_name} ({to_address}):{COLOR.ENDC}")
    print(f"{json.dumps(get_balance(to_account_name, args.hmy_endpoint_src), indent=4)}\n")


@test
def create_validator():
    """
    Returns a dictionary of added validators where key = addr and value = dictionary of associated test data.
    """
    # new_val_count = 3
    new_val_count = 1
    amount = 3  # Must be > 1 b/c of min-self-delegation
    added_validators = {}
    # Sourced from harmony/test/config/local-resharding.txt (Keys must be in provided keystore).
    foundational_node_data = [
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

    funding_account_name = None
    min_funding_acc_funds = new_val_count * amount + (new_val_count * 1)  # +1/new_acc for gas overhead.
    for candidate_acc_names in ACC_NAMES_ADDED:
        if float(get_balance(candidate_acc_names, args.hmy_endpoint_src)[0]["amount"]) > min_funding_acc_funds:
            funding_account_name = candidate_acc_names
            break
    assert funding_account_name is not None, "Imported keys don't have enough funds for the test"

    while not is_after_epoch(0, args.hmy_endpoint_src):
        print("Waiting for epoch 1...")
        time.sleep(5)

    for i, bls_key in enumerate(bls_generator(new_val_count)):
        val_name = f"{ACC_NAME_PREFIX}validator{i}"
        CLI.remove_account(val_name)
        add_key(val_name)
        val_address = CLI.get_address(val_name)
        fund_account(funding_account_name, val_name, amount + 1)  # +1 for gas overhead.

        rates = random.uniform(0, 1), random.uniform(0, 1)
        rate, max_rate = min(rates), max(rates)
        max_change_rate = random.uniform(0, 1)
        max_total_delegation = random.randint(amount, 100)
        proc = CLI.expect_call(f"hmy --node={args.hmy_endpoint_src} staking create-validator "
                               f"--validator-addr {val_address} --name {val_name} "
                               f"--identity test_account --website harmony.one "
                               f"--security-contact Daniel-VDM --details none --rate {rate} --max-rate {max_rate} "
                               f"--max-change-rate {max_change_rate} --min-self-delegation 1 "
                               f"--max-total-delegation {max_total_delegation} "
                               f"--amount {amount} --bls-pubkeys {bls_key['public-key']} "
                               f"--chain-id {args.chain_id} --passphrase={args.passphrase}")
        proc.expect("Enter the absolute path to the encrypted bls private key file:\r\n")
        proc.sendline(bls_key["encrypted-private-key-path"])
        proc.expect("Enter the bls passphrase:\r\n")
        proc.sendline(f"{args.passphrase}")
        txn = json.loads(proc.read().decode())
        print(f"{COLOR.OKGREEN}Created validator {val_address}:{COLOR.ENDC}\n{json.dumps(txn, indent=4)}\n")
        data = {
            "pub-bls-key": bls_key['public-key'],
            "time-created": datetime.datetime.utcnow(),
            "rate": rate,
            "max-rate": max_rate,
            "max_change_rate": max_change_rate,
            "max_total_delegation": max_total_delegation
        }
        added_validators[val_address] = data

    gopath = get_gopath()
    for val_address, key in foundational_node_data:
        val_names = CLI.get_accounts(val_address)
        if val_names and float(get_balance(val_names[0], args.hmy_endpoint_src)[0]["amount"]) < amount + 1:
            key_path = f"{gopath}/src/github.com/harmony-one/harmony/.hmy/{key}.key"
            assert os.path.isfile(key_path)
            rates = random.uniform(0, 1), random.uniform(0, 1)
            rate, max_rate = min(rates), max(rates)
            max_change_rate = random.uniform(0, 1)
            max_total_delegation = random.randint(amount, 100)
            proc = CLI.expect_call(f"hmy --node={args.hmy_endpoint_src} staking create-validator "
                                   f"--validator-addr {val_address} --name {val_names[0]} "
                                   f"--identity test_account --website harmony.one "
                                   f"--security-contact Daniel-VDM --details none --rate {rate} --max-rate {max_rate} "
                                   f"--max-change-rate {max_change_rate} --min-self-delegation 1 "
                                   f"--max-total-delegation {max_total_delegation} "
                                   f"--amount {amount} --bls-pubkeys {key} "
                                   f"--chain-id {args.chain_id} --passphrase={args.passphrase}")
            proc.expect("Enter the absolute path to the encrypted bls private key file:\r\n")
            proc.sendline(key_path)
            proc.expect("Enter the bls passphrase:\r\n")
            proc.sendline("")  # hardcoded passphrase for these bls keys.
            txn = json.loads(proc.read().decode())
            print(f"{COLOR.OKGREEN}Created validator {val_address}:{COLOR.ENDC}\n{json.dumps(txn, indent=4)}\n")
            data = {
                "pub-bls-key": key,
                "time-created": datetime.datetime.utcnow(),
                "rate": rate,
                "max-rate": max_rate,
                "max_change_rate": max_change_rate,
                "max_total_delegation": max_total_delegation
            }
            added_validators[val_address] = data

    return added_validators


@test
def check_validators(added_validators):
    for address, data in added_validators:
        # TODO parse all validators and check them.
        print(f"{COLOR.HEADER}Validator address: {v_addr}{COLOR.ENDC}\n")
        response = json.loads(CLI.single_call(f"hmy --node={args.hmy_endpoint_src} blockchain validator all"))
        print(f"{COLOR.OKGREEN}Current validators:{COLOR.ENDC}\n{json.dumps(response, indent=4)}\n")
        if v_addr not in response["result"]:
            print(f"Validator ({v_addr}) is not on blockchain.")
            return False

    response = json.loads(CLI.single_call(f"hmy --node={args.hmy_endpoint_src} "
                                          f"blockchain validator information {v_addr}"))
    print(f"{COLOR.OKGREEN}Validator ({v_addr}) information:{COLOR.ENDC}\n{json.dumps(response, indent=4)}\n")
    # TODO: check if information matches the create-validator command.
    return True


@test
def create_delegator(val_address):
    account_name = f"{ACC_NAME_PREFIX}delegator"
    add_key(account_name)
    delegator_address = CLI.get_address(account_name)
    funding_account_name = None
    for candidate_acc_names in ACC_NAMES_ADDED:
        if float(get_balance(candidate_acc_names, args.hmy_endpoint_src)[0]["amount"]) > 3:  # 2 + 1 for gas overhead.
            funding_account_name = candidate_acc_names
            break
    assert funding_account_name is not None, "Imported keys don't have enough funds for the test"
    fund_account(funding_account_name, account_name, 2)  # 1 + 1 for gas overhead.

    staking_command = f"hmy staking delegate --validator-addr {val_address} " \
                      f"--delegator-addr {delegator_address} --amount 1 " \
                      f"--node={args.hmy_endpoint_src} " \
                      f"--chain-id={args.chain_id} --passphrase={args.passphrase}"
    response = CLI.single_call(staking_command)
    print(f"\tDelegator transaction response: {response}")

    print(f"Sleeping {args.txn_delay} seconds for finality...\n")
    time.sleep(args.txn_delay)

    return delegator_address


@test
def undelegate(v_address, d_address):
    staking_command = f"hmy staking undelegate --validator-addr {v_address} " \
                      f"--delegator-addr {d_address} --amount 1 " \
                      f"--node={args.hmy_endpoint_src} " \
                      f"--chain-id={args.chain_id} --passphrase={args.passphrase}"
    response = CLI.single_call(staking_command)
    print(f"\tUndelegate transaction response: {response}")

    print(f"Sleeping {args.txn_delay} seconds for finality...\n")
    time.sleep(args.txn_delay)
    return True


@test
def collect_rewards(address):
    print("== Collecting Rewards ==")
    staking_command = f"hmy staking collect-rewards --delegator-addr {address} " \
                      f"--node={args.hmy_endpoint_src} " \
                      f"--chain-id={args.chain_id} --passphrase={args.passphrase}"
    response = CLI.single_call(staking_command)
    print(f"\tCollect rewards transaction response: {response}")

    print(f"Sleeping {args.txn_delay} seconds for finality...\n")
    time.sleep(args.txn_delay)
    return True


@test
def get_delegator_info(v_addr, d_addr):
    print("== Getting Delegator Info by Delegator ==")
    staking_command = f"hmy blockchain delegation by-delegator {d_addr} " \
                      f"--node={args.hmy_endpoint_src}"
    response = CLI.single_call(staking_command)
    print(f"\tDelegator info transaction response: {response} ")

    print("== Getting Delegator Info by Validator ==")
    staking_command = f"hmy blockchain delegation by-validator {v_addr} " \
                      f"--node={args.hmy_endpoint_src}"
    response = CLI.single_call(staking_command)
    print(f"\tDelegator info transaction response: {response}")
    return True


@test
def create_validator_many_keys():
    bls_keys = [d for d in bls_generator(10)]

    for acc in ACC_NAMES_ADDED:
        balance = get_balance(acc, args.hmy_endpoint_src)
        if balance[0]["amount"] < 1:
            continue
        address = CLI.get_address(acc)
        key_counts = [1, 10]
        for i in key_counts:
            bls_key_string = ','.join(el["public-key"] for el in bls_keys[:i])
            staking_command = f"hmy staking create-validator --amount 1 --validator-addr {address} " \
                              f"--bls-pubkeys {bls_key_string} --identity foo --details bar --name baz " \
                              f"--max-change-rate 0.1 --max-rate 0.2 --max-total-delegation 10 " \
                              f"--min-self-delegation 1 --rate 0.1 --security-contact Leo  " \
                              f"--website harmony.one --node={args.hmy_endpoint_src} " \
                              f"--chain-id={args.chain_id} --passphrase={args.passphrase}"
            response = CLI.single_call(staking_command)
            print(f"\nPassed creating a validator with {i} bls key(s)")
            print(f"\tCLI command: {staking_command}")
            print(f"\tStaking transaction response: {response}")
            if i == key_counts[-1]:
                return
            print(f"Sleeping {args.txn_delay} seconds for finality...\n")
            time.sleep(args.txn_delay)

    print("Failed CLI staking test.")
    sys.exit(-1)


@announce
def get_raw_txn(passphrase, chain_id, node, src_shard, dst_shard):
    """
    Must be cross shard transaction for tests.

    If importing keys using 'import-ks', no passphrase is needed.
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
    source_shard = args.src_shard if args.src_shard else get_shard_from_endpoint(args.hmy_endpoint_src)
    destination_shard = args.dst_shard if args.dst_shard else get_shard_from_endpoint(args.hmy_endpoint_dst)
    raw_txn = get_raw_txn(passphrase=args.passphrase, chain_id=args.chain_id,
                          node=args.hmy_endpoint_src, src_shard=source_shard, dst_shard=destination_shard)

    if str(source_shard) not in args.hmy_endpoint_src:
        print(f"Source shard {source_shard} may not match source endpoint {args.hmy_endpoint_src}")
    if str(destination_shard) not in args.hmy_endpoint_dst:
        print(f"Destination shard {destination_shard} may not match destination endpoint {args.hmy_endpoint_dst}")

    for i, var in enumerate(env_json["values"]):
        if var["key"] == "rawTransaction":
            env_json["values"][i]["value"] = raw_txn
        if var["key"] == "txn_delay":
            env_json["values"][i]["value"] = args.txn_delay

    for i, var in enumerate(global_json["values"]):
        if var["key"] == "hmy_endpoint_src":
            global_json["values"][i]["value"] = args.hmy_endpoint_src
        if var["key"] == "hmy_endpoint_dst":
            global_json["values"][i]["value"] = args.hmy_endpoint_dst


@announce
def setup_newman_only_explorer(test_json, global_json, env_json):
    if "localhost" in args.hmy_endpoint_src or "localhost" in args.hmy_exp_endpoint:
        print("\n\t[WARNING] This test is for testnet or mainnet.\n")

    source_shard = args.src_shard if args.src_shard else get_shard_from_endpoint(args.hmy_endpoint_src)
    destination_shard = args.dst_shard if args.dst_shard else get_shard_from_endpoint(args.hmy_endpoint_dst)
    raw_txn = get_raw_txn(passphrase=args.passphrase, chain_id=args.chain_id,
                          node=args.hmy_endpoint_src, src_shard=source_shard, dst_shard=destination_shard)

    if str(source_shard) not in args.hmy_endpoint_src:
        print(f"Source shard {source_shard} may not match source endpoint {args.hmy_endpoint_src}")
    if str(destination_shard) not in args.hmy_endpoint_dst:
        print(f"Destination shard {destination_shard} may not match destination endpoint {args.hmy_endpoint_dst}")

    for i, var in enumerate(env_json["values"]):
        if var["key"] == "rawTransaction":
            env_json["values"][i]["value"] = raw_txn
        if var["key"] == "tx_beta_endpoint":
            env_json["values"][i]["value"] = args.hmy_exp_endpoint
        if var["key"] == "txn_delay":
            env_json["values"][i]["value"] = args.txn_delay
        if var["key"] == "source_shard":
            env_json["values"][i]["value"] = source_shard

    for i, var in enumerate(global_json["values"]):
        if var["key"] == "hmy_exp_endpoint":
            global_json["values"][i]["value"] = args.hmy_exp_endpoint
        if var["key"] == "hmy_endpoint_src":
            global_json["values"][i]["value"] = args.hmy_endpoint_src


@announce
def setup_newman_default(test_json, global_json, env_json):
    if "localhost" in args.hmy_endpoint_src or "localhost" in args.hmy_exp_endpoint:
        print("\n\t[WARNING] This test is for testnet or mainnet.\n")

    source_shard = args.src_shard if args.src_shard else get_shard_from_endpoint(args.hmy_endpoint_src)
    destination_shard = args.dst_shard if args.dst_shard else get_shard_from_endpoint(args.hmy_endpoint_dst)
    raw_txn = get_raw_txn(passphrase=args.passphrase, chain_id=args.chain_id,
                          node=args.hmy_endpoint_src, src_shard=source_shard, dst_shard=destination_shard)

    if str(source_shard) not in args.hmy_endpoint_src:
        print(f"Source shard {source_shard} may not match source endpoint {args.hmy_endpoint_src}")
    if str(destination_shard) not in args.hmy_endpoint_dst:
        print(f"Destination shard {destination_shard} may not match destination endpoint {args.hmy_endpoint_dst}")

    for i, var in enumerate(env_json["values"]):
        if var["key"] == "rawTransaction":
            env_json["values"][i]["value"] = raw_txn
        if var["key"] == "tx_beta_endpoint":
            env_json["values"][i]["value"] = args.hmy_exp_endpoint
        if var["key"] == "txn_delay":
            env_json["values"][i]["value"] = args.txn_delay
        if var["key"] == "source_shard":
            env_json["values"][i]["value"] = source_shard

    for i, var in enumerate(global_json["values"]):
        if var["key"] == "hmy_endpoint_src":
            global_json["values"][i]["value"] = args.hmy_endpoint_src
        if var["key"] == "hmy_endpoint_dst":
            global_json["values"][i]["value"] = args.hmy_endpoint_dst
        if var["key"] == "hmy_exp_endpoint":
            global_json["values"][i]["value"] = args.hmy_exp_endpoint


if __name__ == "__main__":
    args = parse_args()
    CLI = pyhmy.HmyCLI(environment=pyhmy.get_environment(), hmy_binary_path=args.hmy_binary_path)
    assert os.path.isfile(CLI.hmy_binary_path), "CLI binary is not found, specify it with option."
    version_str = re.search('version v.*-', CLI.version).group(0).split('-')[0].replace("version v", "")
    assert int(version_str) >= 170, "CLI binary is the wrong version."
    assert os.path.isdir(args.keys_dir), "Could not find keystore directory"
    assert is_active_shard(args.hmy_endpoint_src), "The source shard endpoint is NOT active."
    # assert is_active_shard(args.hmy_endpoint_dst), "The destination shard endpoint is NOT active."
    if args.chain_id not in json.loads(CLI.single_call("hmy blockchain known-chains")):
        args.chain_id = "testnet"
    exit_code = 0

    try:
        load_keys()

        print(f"Waiting for epoch {args.start_epoch} (or later)")
        while not is_after_epoch(args.start_epoch - 1, args.hmy_endpoint_src):
            time.sleep(5)

        if not args.ignore_staking_test:
            test_validators = create_validator()
            check_validators(test_validators)
            v_addr = random.choice(list(test_validators.keys()))
            # edit_validator(test_validators)
            d_addr = create_delegator(v_addr)
            check_validators(d_addr)
            undelegate(v_addr, d_addr)
            collect_rewards(d_addr)
            # create_validator_many_keys()

        if not args.ignore_regression_test:
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

            for n in range(args.iterations):
                print(f"\n\tIteration {n+1} out of {args.iterations}\n")
                proc = subprocess.Popen(["newman", "run", f"{args.test_dir}/test.json",
                                         "-e", f"{args.test_dir}/env.json",
                                         "-g", f"{args.test_dir}/global.json"])
                proc.wait()
                exit_code = proc.returncode
                if proc.returncode == 0:
                    print(f"\n\tSucceeded in {n+1} attempt(s)\n")
                    break

    except (RuntimeError, KeyboardInterrupt) as err:
        print("Removing imported keys from CLI's keystore...")
        for acc in ACC_NAMES_ADDED:
            CLI.remove_account(acc)
        raise err

    print("Removing imported keys from CLI's keystore...")
    for acc in ACC_NAMES_ADDED:
        CLI.remove_account(acc)
    sys.exit(exit_code)
