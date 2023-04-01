import os
import sys

import pytest
import pytest_asyncio

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
from pathlib import Path
from typing import List

from blspy import AugSchemeMPL, PrivateKey
from cdv.cmds.rpc import get_client
from cdv.cmds.sim_utils import SIMULATOR_ROOT_PATH
from cdv.test import setup as setup_test
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.rpc.full_node_rpc_client import FullNodeRpcClient
from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.simulator.simulator_full_node_rpc_client import \
    SimulatorFullNodeRpcClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import (INFINITE_COST, Program)
from chia.types.blockchain_format.serialized_program import SerializedProgram
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.types.condition_opcodes import ConditionOpcode
from chia.types.spend_bundle import SpendBundle
from chia.util.bech32m import (bech32_decode, bech32_encode, convertbits,
                               decode_puzzle_hash, encode_puzzle_hash)
from chia.util.condition_tools import conditions_dict_for_solution
from chia.util.config import load_config
from chia.util.hash import std_hash
from chia.util.ints import uint16, uint32, uint64
from chia.wallet.cat_wallet.cat_utils import (
    SpendableCAT,
    construct_cat_puzzle,
    get_innerpuzzle_from_puzzle,
    unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.derive_keys import master_sk_to_wallet_sk_unhardened
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.cat_loader import CAT_MOD, CAT_MOD_HASH
from chia.wallet.puzzles.p2_conditions import puzzle_for_conditions
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    DEFAULT_HIDDEN_PUZZLE_HASH,
    calculate_synthetic_secret_key,
    puzzle_for_pk,
    puzzle_for_synthetic_public_key,
    solution_for_delegated_puzzle,
)
from chia.wallet.puzzles.singleton_top_layer_v1_1 import (
    SINGLETON_LAUNCHER,
    SINGLETON_LAUNCHER_HASH,
    SINGLETON_MOD,
    SINGLETON_MOD_HASH,
    generate_launcher_coin,
    launch_conditions_and_coinsol,
    lineage_proof_for_coinsol,
    puzzle_for_singleton,
    solution_for_singleton,
)
from chia.wallet.puzzles.tails import GenesisById
from chia.wallet.sign_coin_spends import sign_coin_spends
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH, Offer
from chia.wallet.util.puzzle_compression import (
    compress_object_with_puzzles,
    decompress_object_with_puzzles,
    lowest_best_version,
)
from chia.wallet.util.wallet_types import WalletType
from chia_rs import run_chia_program
from clvm.casts import int_to_bytes
from chia.simulator.setup_nodes import setup_simulators_and_wallets
from chia.types.peer_info import PeerInfo
from chia.simulator.simulator_protocol import FarmNewBlockProtocol
from chia.rpc.wallet_rpc_api import WalletRpcApi
from chia.simulator.simulator_full_node_rpc_api import SimulatorFullNodeRpcApi
from chia.rpc.rpc_server import start_rpc_server

from clvm import SExp
from private_key_things import *
from tibet_lib import *
from secrets import token_bytes


class TestTibetSwap:
    async def wait_for_wallet_sync(self, wallet_client):
        synced = await wallet_client.get_synced()
        print(synced) # TODO: debug
        while not synced:
            time.sleep(0.5)
            synced = await wallet_client.get_synced()
            print(synced) # TODO: debug
    

    async def wait_for_full_node_sync(self, full_node_client):
        blockchain_state = await full_node_client.get_blockchain_state()
        while not blockchain_state['sync']['synced']:
            time.sleep(0.5)
            blockchain_state = await full_node_client.get_blockchain_state()


    # thank you trepca for this function!
    @pytest_asyncio.fixture(scope="function")
    async def node_and_wallet(self):
        sims = setup_simulators_and_wallets(1, 1, {})
        async for _ in sims:
            yield _


    # thank you trepca for this function!
    @pytest_asyncio.fixture(scope="function")
    async def setup(self, node_and_wallet):
        full_nodes, wallets, bt = node_and_wallet
    
        full_node_api: FullNodeSimulator = full_nodes[0]
        full_node_server = full_node_api.server

        wallet_node_maker, server_0 = wallets[0]
        wallet_maker: Wallet = wallet_node_maker.wallet_state_manager.main_wallet

        ph_maker = await wallet_maker.get_new_puzzlehash()
        ph_token = bytes32(token_bytes(32))

        wallet_node_maker.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }

        await server_0.start_client(PeerInfo("127.0.0.1", uint16(full_node_server._port)), None)

        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_maker))
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_token))

        api_maker = WalletRpcApi(wallet_node_maker)
        config = bt.config
        daemon_port = config["daemon_port"]
        self_hostname = config["self_hostname"]

        def stop_node_cb() -> None:
            pass

        full_node_rpc_api = SimulatorFullNodeRpcApi(full_node_api.full_node)

        rpc_server_node = await start_rpc_server(
            full_node_rpc_api,
            self_hostname,
            daemon_port,
            uint16(0),
            stop_node_cb,
            bt.root_path,
            config,
            connect_to_daemon=False,
        )

        rpc_server_maker = await start_rpc_server(
            api_maker,
            self_hostname,
            daemon_port,
            uint16(0),
            lambda x: None,  # type: ignore
            bt.root_path,
            config,
            connect_to_daemon=False,
        )

        client_wallet: WalletRpcClient = await WalletRpcClient.create(
            self_hostname, rpc_server_maker.listen_port, bt.root_path, config
        )
        client_node: SimulatorFullNodeRpcClient = await SimulatorFullNodeRpcClient.create(
            self_hostname, rpc_server_node.listen_port, bt.root_path, config
        )
        
        # do not judge this part, ok?
        alice_passphrase = "measure weapon fun decade glue hole toe virtual add talk muffin melt bubble wire casino adult leave guide seed ridge enough badge will section"
        bob_passphrase = "check disease raw gate drip also time puzzle subway margin scout trade all cotton shadow patch dawn shell imitate vacuum exist pact deal surprise"
        charlie_passphrase = "test banner actor ginger latin mean other window solid orchard ordinary park tobacco maple shadow love hood boat pilot pact wing heart cupboard daring"
        
        alice_resp = await client_wallet.add_key(alice_passphrase.split(" "))
        assert alice_resp['success']
        alice_fingerprint = alice_resp['fingerprint']
        
        bob_resp = await client_wallet.add_key(bob_passphrase.split(" "))
        assert bob_resp['success']
        bob_fingerprint = alice_resp['fingerprint']

        charlie_resp = await client_wallet.add_key(charlie_passphrase.split(" "))
        assert charlie_resp['success']
        charlie_fingerprint = alice_resp['fingerprint']

        async def switch_to_fingerprint(client_wallet, fingerprint):
            await client_wallet.log_in(int(fingerprint))
            await self.wait_for_wallet_sync(client_wallet)

        async def switch_to_alice(client_wallet):
            await switch_to_fingerprint(client_wallet, alice_fingerprint)

        async def switch_to_bob(wallet_client):
            await client_wallet(client_wallet, bob_fingerprint)

        async def switch_to_charlie(client_wallet):
            await switch_to_fingerprint(client_wallet, charlie_fingerprint)

        await switch_to_charlie(client_wallet)
        # address = await client_wallet.get_next_address(1, True) # wallet id = 1, new address = true
        # await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_token))
        
        # await switch_to_bob(client_wallet)
        # address = await client_wallet.get_next_address(1, True) # wallet id = 1, new address = true
        # await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_token))
        
        # await switch_to_alice(client_wallet)
        # address = await client_wallet.get_next_address(1, True) # wallet id = 1, new address = true
        # await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_token))
        
        # await self.wait_for_wallet_sync(client_wallet)
        # await self.wait_for_full_node_sync(client_node)

        yield client_node, client_wallet, switch_to_alice, switch_to_bob, switch_to_charlie

        client_wallet.close()
        client_node.close()
        rpc_server_maker.close()
        rpc_server_node.close()
        await client_wallet.await_closed()
        await client_node.await_closed()
        await rpc_server_maker.await_closed()
        await rpc_server_node.await_closed()


    @pytest.mark.asyncio
    async def test_healthz(self, setup):
        full_node_client, wallet_client, _, __, ___ = setup
        
        full_node_resp = await full_node_client.healthz()
        assert full_node_resp['success']

        wallet_resp = await wallet_client.healthz()
        assert wallet_resp['success']


    # def get_created_coins_from_coin_spend(self, cs):
    #     coins = []

    #     _, conditions_dict, __ = conditions_dict_for_solution(
    #         cs.puzzle_reveal,
    #         cs.solution,
    #         INFINITE_COST
    #     )

    #     for cwa in conditions_dict[ConditionOpcode.CREATE_COIN]:
    #         coins.append(Coin(
    #             cs.coin.name(),
    #             cwa.vars[0], # puzzle hash of created coin
    #             SExp.to(cwa.vars[1]).as_int()
    #         ))

    #     return coins


    # async def select_standard_coin_and_puzzle(self, wallet_client, amount):
    #     spendable_coins = await wallet_client.get_spendable_coins(1, min_coin_amount=amount) # wallet id 1, amount amount
        
    #     coin_puzzle = None
    #     index = 0
        
    #     retries = 0
    #     while coin_puzzle is None:
    #         try:
    #             coin = spendable_coins[0][index].coin
    #             coin_puzzle = await get_standard_coin_puzzle(wallet_client, coin)
    #             index += 1
    #         except:
    #             spendable_coins = await wallet_client.get_spendable_coins(1, min_coin_amount=amount) # wallet id 1, amount amount
    #             index = 0
    #             retries += 1
    #             if retries > 3:
    #                 print("ok, won't find a coin any time soon :(")
    #                 spendable_coins[0][31337][":("]
    #             else:
    #                 time.sleep(5)

    #     return coin, coin_puzzle


    # async def launch_router(self, wallet_client, full_node_client):
    #     coin, coin_puzzle = await self.select_standard_coin_and_puzzle(wallet_client, 2)

    #     launcher_id, sb = await launch_router_from_coin(coin, coin_puzzle)

    #     signed_sb = await sign_spend_bundle(wallet_client, sb)
    #     resp = await full_node_client.push_tx(signed_sb)

    #     assert resp["success"]
    #     await self.wait_for_full_node_sync(full_node_client)

    #     router_launch_coin_spend = None
    #     router_current_coin = None

    #     for cs in signed_sb.coin_spends:
    #         if cs.coin.puzzle_hash == SINGLETON_LAUNCHER_HASH:
    #             router_launch_coin_spend = cs
    #             router_current_coin = self.get_created_coins_from_coin_spend(cs)[0]

    #     return bytes.fromhex(launcher_id), router_current_coin, router_launch_coin_spend


    # async def create_test_cat(self, wallet_client, full_node_client, token_amount=1000000):
    #     coin, coin_puzzle = await self.select_standard_coin_and_puzzle(wallet_client, token_amount)
        
    #     tail_hash, sb = await create_test_cat(token_amount, coin, coin_puzzle)

    #     signed_sb = await sign_spend_bundle(wallet_client, sb)
    #     resp = await full_node_client.push_tx(signed_sb)

    #     assert resp["success"]
    #     await self.wait_for_full_node_sync(full_node_client)

    #     return bytes.fromhex(tail_hash)


    # async def create_pair(
    #     self,
    #     wallet_client,
    #     full_node_client,
    #     router_launcher_id,
    #     tail_hash,
    #     current_router_coin,
    #     current_router_coin_creation_spend
    # ):
    #     coin, coin_puzzle = await self.select_standard_coin_and_puzzle(wallet_client, 2)

    #     pair_launcher_id, sb = await create_pair_from_coin(
    #         coin,
    #         coin_puzzle,
    #         tail_hash,
    #         router_launcher_id,
    #         current_router_coin,
    #         current_router_coin_creation_spend
    #     )

    #     signed_sb = await sign_spend_bundle(wallet_client, sb)
    #     resp = await full_node_client.push_tx(signed_sb)

    #     assert resp["success"]
    #     await self.wait_for_full_node_sync(full_node_client)

    #     pair_coin = None
    #     pair_coin_creation_spend = None
    #     router_new_coin = None
    #     router_new_coin_creation_spend = None

    #     for cs in sb.coin_spends:
    #         if cs.coin.puzzle_hash == SINGLETON_LAUNCHER_HASH:
    #             pair_coin_creation_spend = cs
    #             pair_coin = self.get_created_coins_from_coin_spend(cs)[0]
    #         elif cs.coin.amount == 1:
    #             possible_coins = self.get_created_coins_from_coin_spend(cs)
    #             if len(possible_coins) == 2 and possible_coins[0].amount + possible_coins[1].amount == 3:
    #                 router_new_coin_creation_spend = cs
    #                 for pc in possible_coins:
    #                     if pc.amount == 1:
    #                         router_new_coin = pc

    #     return bytes.fromhex(pair_launcher_id), pair_coin, pair_coin_creation_spend, router_new_coin, router_new_coin_creation_spend


    # async def get_wallet_id_for_cat(self, wallet_client, tail_hash):
    #     wallets = await wallet_client.get_wallets(wallet_type = WalletType.CAT)
    #     wallet_id = next((_['id'] for _ in wallets if _['data'].startswith(tail_hash.hex())), None)

    #     if wallet_id is None:
    #         await wallet_client.create_wallet_for_existing_cat(tail_hash) # create wallet
    #         time.sleep(10) # I don't have any other solution, ok?!
    #         await self.wait_for_wallet_sync(wallet_client)
    #         return await self.get_wallet_id_for_cat(wallet_client, tail_hash)

    #     return int(wallet_id)
            

    # async def get_balance(self, wallet_client, tail_hash_or_none = None):
    #     await self.wait_for_wallet_sync(wallet_client)

    #     wallet_id = 1 # XCH
    #     if tail_hash_or_none is not None:
    #         wallet_id = await self.get_wallet_id_for_cat(wallet_client, tail_hash_or_none)

    #     resp = await wallet_client.get_wallet_balance(wallet_id)
    #     return resp["spendable_balance"]


    # @pytest.mark.asyncio
    # async def test_router_launch(self, setup):
    #     full_node_client, wallet_client, switch_to_alice, switch_to_bob, switch_to_charlie = setup
    #     try:
    #         launcher_id, _, __ = await self.launch_router(wallet_client, full_node_client)

    #         cr = await full_node_client.get_coin_record_by_name(launcher_id)
    #         assert cr is not None
    #         assert cr.spent
    #     finally:
    #         full_node_client.close()
    #         wallet_client.close()
    #         await full_node_client.await_closed()
    #         await wallet_client.await_closed()


    # @pytest.mark.asyncio
    # async def test_pair_creation(self, setup):
    #     full_node_client, wallet_client, switch_to_alice, switch_to_bob, switch_to_charlie = setup
    #     try:
    #         router_launcher_id, current_router_coin, router_creation_spend = await self.launch_router(
    #             wallet_client, full_node_client
    #         )
            
    #         tail_hash = await self.create_test_cat(wallet_client, full_node_client)

    #         pair_launcher_id, current_pair_coin, pair_creation_spend, current_router_coin, router_creation_spend = await self.create_pair(
    #             wallet_client,
    #             full_node_client,
    #             router_launcher_id,
    #             tail_hash,
    #             current_router_coin,
    #             router_creation_spend
    #         )
    #         cr = await full_node_client.get_coin_record_by_name(pair_launcher_id)
    #         assert cr is not None
    #         assert cr.spent

    #         # another pair, just to be sure
    #         tail_hash2 = await self.create_test_cat(wallet_client, full_node_client)

    #         pair2_launcher_id, current_pair_coin, pair_creation_spend, current_router_coin, router_creation_spend = await self.create_pair(
    #             wallet_client,
    #             full_node_client,
    #             router_launcher_id,
    #             tail_hash2,
    #             current_router_coin,
    #             router_creation_spend
    #         )
    #         cr = await full_node_client.get_coin_record_by_name(pair2_launcher_id)
    #         assert cr is not None
    #         assert cr.spent
    #     finally:
    #         full_node_client.close()
    #         wallet_client.close()
    #         await full_node_client.await_closed()
    #         await wallet_client.await_closed()


    # @pytest.mark.asyncio
    # async def test_pair_operations(self, setup):
    #     full_node_client, wallet_client, switch_to_alice, switch_to_bob, switch_to_charlie = setup
    #     try:
    #         router_launcher_id, current_router_coin, router_creation_spend = await self.launch_router(
    #             wallet_client, full_node_client
    #         )
            
    #         token_total_supply = 1000000 * 1000 # in mojos
    #         token_tail_hash = await self.create_test_cat(wallet_client, full_node_client, token_amount=token_total_supply // 1000)
            
    #         pair_launcher_id, current_pair_coin, pair_creation_spend, current_router_coin, router_creation_spend = await self.create_pair(
    #             wallet_client,
    #             full_node_client,
    #             router_launcher_id,
    #             token_tail_hash,
    #             current_router_coin,
    #             router_creation_spend
    #         )
            
    #         pair_liquidity_tail_hash = pair_liquidity_tail_puzzle(pair_launcher_id).get_tree_hash()
            
    #         assert (await self.get_balance(wallet_client, token_tail_hash)) == token_total_supply
    #         assert (await self.get_balance(wallet_client, pair_liquidity_tail_hash)) == 0

    #         xch_balance_before_all_ops = xch_balance_before = await self.get_balance(wallet_client)

    #         # 1. Deposit liquidity: 1000 CAT mojos and 100000000 mojos
    #         # python3 tibet.py deposit-liquidity --xch-amount 100000000 --token-amount 1000 --asset-id [asset_id] --push-tx
    #         token_wallet_id = await self.get_wallet_id_for_cat(wallet_client, token_tail_hash)
    #         liquidity_wallet_id = await self.get_wallet_id_for_cat(wallet_client, pair_liquidity_tail_hash)

    #         xch_amount = 100000000
    #         token_amount = 1000
    #         liquidity_token_amount = token_amount # initial deposit

    #         offer_dict = {}
    #         offer_dict[1] = - xch_amount - liquidity_token_amount # also for liqiudity TAIL creation
    #         offer_dict[token_wallet_id] = -token_amount
    #         offer_dict[liquidity_wallet_id] = liquidity_token_amount
    #         offer_resp = await wallet_client.create_offer_for_ids(offer_dict)
    #         offer = offer_resp[0]
    #         offer_str = offer.to_bech32()

    #         # get pair state, even though it's 0 - we need to test teh func-tion!
    #         current_pair_coin, pair_creation_spend, pair_state, sb_to_aggregate, _ = await sync_pair(
    #             full_node_client, current_pair_coin.name(), token_tail_hash
    #         )
    #         assert pair_state["liquidity"] == 0
    #         assert pair_state["xch_reserve"] == 0
    #         assert pair_state["token_reserve"] == 0

    #         # there are no reserves at this point, but the function should be tested nonetheless
    #         xch_reserve_coin, token_reserve_coin, token_reserve_lineage_proof = await get_pair_reserve_info(
    #             full_node_client,
    #             pair_launcher_id,
    #             current_pair_coin,
    #             token_tail_hash,
    #             pair_creation_spend,
    #             sb_to_aggregate
    #         )

    #         sb = await respond_to_deposit_liquidity_offer(
    #             pair_launcher_id,
    #             current_pair_coin,
    #             pair_creation_spend,
    #             token_tail_hash,
    #             pair_state["liquidity"],
    #             pair_state["xch_reserve"],
    #             pair_state["token_reserve"],
    #             offer_str,
    #             xch_reserve_coin,
    #             token_reserve_coin,
    #             token_reserve_lineage_proof
    #         )

    #         resp = await full_node_client.push_tx(sb)

    #         assert resp["success"]
    #         await self.wait_for_full_node_sync(full_node_client)

    #         xch_balance_now = await self.get_balance(wallet_client)
    #         assert xch_balance_before - xch_balance_now == xch_amount + liquidity_token_amount

    #         token_balance_now = await self.get_balance(wallet_client, token_tail_hash)
    #         assert token_total_supply - token_balance_now == token_amount

    #         assert await self.get_balance(wallet_client, pair_liquidity_tail_hash) == liquidity_token_amount

    #         # 2. Deposit moar liquidity (worth 4000 tokens, so 4000 token mojos and 100000000 mojos)
    #         # python3 tibet.py deposit-liquidity --token-amount 4000 --asset-id [asset_id] --push-tx
    #         xch_balance_before = xch_balance_now
    #         token_balance_before = token_balance_now
    #         liquidity_balance_before = liquidity_token_amount

    #         xch_amount = 400000000
    #         token_amount = 4000
    #         liquidity_token_amount = 4000 # 1:1

    #         offer_dict = {}
    #         offer_dict[1] = - xch_amount - liquidity_token_amount # also for liqiudity TAIL creation
    #         offer_dict[token_wallet_id] = -token_amount
    #         offer_dict[liquidity_wallet_id] = liquidity_token_amount
    #         offer_resp = await wallet_client.create_offer_for_ids(offer_dict)
    #         offer = offer_resp[0]
    #         offer_str = offer.to_bech32()

    #         current_pair_coin, pair_creation_spend, pair_state, sb_to_aggregate, _ = await sync_pair(
    #             full_node_client, current_pair_coin.name(), token_tail_hash
    #         )
    #         assert pair_state["liquidity"] == 1000
    #         assert pair_state["xch_reserve"] == 100000000
    #         assert pair_state["token_reserve"] == 1000

    #         xch_reserve_coin, token_reserve_coin, token_reserve_lineage_proof = await get_pair_reserve_info(
    #             full_node_client,
    #             pair_launcher_id,
    #             current_pair_coin,
    #             token_tail_hash,
    #             pair_creation_spend,
    #             sb_to_aggregate
    #         )

    #         sb = await respond_to_deposit_liquidity_offer(
    #             pair_launcher_id,
    #             current_pair_coin,
    #             pair_creation_spend,
    #             token_tail_hash,
    #             pair_state["liquidity"],
    #             pair_state["xch_reserve"],
    #             pair_state["token_reserve"],
    #             offer_str,
    #             xch_reserve_coin,
    #             token_reserve_coin,
    #             token_reserve_lineage_proof
    #         )

    #         resp = await full_node_client.push_tx(sb)

    #         assert resp["success"]
    #         await self.wait_for_full_node_sync(full_node_client)

    #         xch_balance_now = await self.get_balance(wallet_client)
    #         assert xch_balance_before - xch_balance_now == xch_amount + liquidity_token_amount

    #         token_balance_now = await self.get_balance(wallet_client, token_tail_hash)
    #         assert token_balance_before - token_balance_now == token_amount

    #         liquidity_balance_now = await self.get_balance(wallet_client, pair_liquidity_tail_hash)
    #         assert liquidity_balance_now - liquidity_balance_before == liquidity_token_amount

    #         # 3. Withdraw 800 liquidity tokens
    #         # python3 tibet.py remove-liquidity --liquidity-token-amount 800 --asset-id [asset_id] --push-tx
    #         xch_balance_before = xch_balance_now
    #         token_balance_before = token_balance_now
    #         liquidity_balance_before = liquidity_balance_now

    #         xch_amount = 80000000
    #         token_amount = 800
    #         liquidity_token_amount = 800 # 1:1

    #         offer_dict = {}
    #         offer_dict[1] = xch_amount + liquidity_token_amount # also ask for xch from liquidity cat burn
    #         offer_dict[token_wallet_id] = token_amount
    #         offer_dict[liquidity_wallet_id] = -liquidity_token_amount
    #         offer_resp = await wallet_client.create_offer_for_ids(offer_dict)
    #         offer = offer_resp[0]
    #         offer_str = offer.to_bech32()

    #         current_pair_coin, pair_creation_spend, pair_state, sb_to_aggregate, _ = await sync_pair(
    #             full_node_client, current_pair_coin.name(), token_tail_hash
    #         )
    #         assert pair_state["liquidity"] == 5000
    #         assert pair_state["xch_reserve"] == 500000000
    #         assert pair_state["token_reserve"] == 5000

    #         xch_reserve_coin, token_reserve_coin, token_reserve_lineage_proof = await get_pair_reserve_info(
    #             full_node_client,
    #             pair_launcher_id,
    #             current_pair_coin,
    #             token_tail_hash,
    #             pair_creation_spend,
    #             sb_to_aggregate
    #         )

    #         sb = await respond_to_remove_liquidity_offer(
    #             pair_launcher_id,
    #             current_pair_coin,
    #             pair_creation_spend,
    #             token_tail_hash,
    #             pair_state["liquidity"],
    #             pair_state["xch_reserve"],
    #             pair_state["token_reserve"],
    #             offer_str,
    #             xch_reserve_coin,
    #             token_reserve_coin,
    #             token_reserve_lineage_proof
    #         )

    #         resp = await full_node_client.push_tx(sb)

    #         assert resp["success"]
    #         await self.wait_for_full_node_sync(full_node_client)

    #         xch_balance_now = await self.get_balance(wallet_client)
    #         assert xch_balance_now - xch_balance_before == xch_amount + liquidity_token_amount

    #         token_balance_now = await self.get_balance(wallet_client, token_tail_hash)
    #         assert token_balance_now - token_balance_before == token_amount

    #         liquidity_balance_now = await self.get_balance(wallet_client, pair_liquidity_tail_hash)
    #         assert liquidity_balance_before - liquidity_balance_now == liquidity_token_amount

    #         # 4. Change 100000000 XCH to tokens
    #         # python3 tibet.py xch-to-token --xch-amount 100000000 --asset-id [asset_id] --push-tx
    #         xch_balance_before = xch_balance_now
    #         token_balance_before = token_balance_now
    #         liquidity_balance_before = liquidity_balance_now

    #         current_pair_coin, pair_creation_spend, pair_state, sb_to_aggregate, _ = await sync_pair(
    #             full_node_client, current_pair_coin.name(), token_tail_hash
    #         )
    #         assert pair_state["liquidity"] == 4200
    #         assert pair_state["xch_reserve"] == 420000000
    #         assert pair_state["token_reserve"] == 4200

    #         xch_reserve_coin, token_reserve_coin, token_reserve_lineage_proof = await get_pair_reserve_info(
    #             full_node_client,
    #             pair_launcher_id,
    #             current_pair_coin,
    #             token_tail_hash,
    #             pair_creation_spend,
    #             sb_to_aggregate
    #         )

    #         xch_amount = 100000000
    #         token_amount = pair_state["token_reserve"] * xch_amount * 993 // (1000 * pair_state["xch_reserve"] + 993 * xch_amount)

    #         offer_dict = {}
    #         offer_dict[1] = -xch_amount # offer XCH
    #         offer_dict[token_wallet_id] = token_amount # ask for token
    #         offer_resp = await wallet_client.create_offer_for_ids(offer_dict)
    #         offer = offer_resp[0]
    #         offer_str = offer.to_bech32()

    #         sb = await respond_to_swap_offer(
    #            pair_launcher_id,
    #             current_pair_coin,
    #             pair_creation_spend,
    #             token_tail_hash,
    #             pair_state["liquidity"],
    #             pair_state["xch_reserve"],
    #             pair_state["token_reserve"],
    #             offer_str,
    #             xch_reserve_coin,
    #             token_reserve_coin,
    #             token_reserve_lineage_proof
    #         )

    #         resp = await full_node_client.push_tx(sb)

    #         assert resp["success"]
    #         await self.wait_for_full_node_sync(full_node_client)

    #         xch_balance_now = await self.get_balance(wallet_client)
    #         assert xch_balance_before - xch_balance_now == xch_amount

    #         token_balance_now = await self.get_balance(wallet_client, token_tail_hash)
    #         assert token_balance_now - token_balance_before == token_amount

    #         liquidity_balance_now = await self.get_balance(wallet_client, pair_liquidity_tail_hash)
    #         assert liquidity_balance_before == liquidity_balance_before

    #         # 5. Change 1000 tokens to XCH
    #         # python3 tibet.py token-to-xch --token-amount 1000 --asset-id [asset_id] --push-tx
    #         xch_balance_before = xch_balance_now
    #         token_balance_before = token_balance_now
    #         liquidity_balance_before = liquidity_balance_now

    #         current_pair_coin, pair_creation_spend, pair_state, sb_to_aggregate, _ = await sync_pair(
    #             full_node_client, current_pair_coin.name(), token_tail_hash
    #         )
    #         assert pair_state["liquidity"] == 4200
    #         assert pair_state["xch_reserve"] == 420000000 + xch_amount
    #         assert pair_state["token_reserve"] == 4200 - token_amount

    #         xch_reserve_coin, token_reserve_coin, token_reserve_lineage_proof = await get_pair_reserve_info(
    #             full_node_client,
    #             pair_launcher_id,
    #             current_pair_coin,
    #             token_tail_hash,
    #             pair_creation_spend,
    #             sb_to_aggregate
    #         )

    #         token_amount = 1000
    #         xch_amount = pair_state["xch_reserve"] * token_amount * 993 // (1000 * pair_state["token_reserve"] + 993 * token_amount)

    #         offer_dict = {}
    #         offer_dict[1] = xch_amount # ask for XCH
    #         offer_dict[token_wallet_id] = -token_amount # offer token
    #         offer_resp = await wallet_client.create_offer_for_ids(offer_dict)
    #         offer = offer_resp[0]
    #         offer_str = offer.to_bech32()

    #         sb = await respond_to_swap_offer(
    #            pair_launcher_id,
    #             current_pair_coin,
    #             pair_creation_spend,
    #             token_tail_hash,
    #             pair_state["liquidity"],
    #             pair_state["xch_reserve"],
    #             pair_state["token_reserve"],
    #             offer_str,
    #             xch_reserve_coin,
    #             token_reserve_coin,
    #             token_reserve_lineage_proof
    #         )

    #         resp = await full_node_client.push_tx(sb)

    #         assert resp["success"]
    #         await self.wait_for_full_node_sync(full_node_client)

    #         xch_balance_now = await self.get_balance(wallet_client)
    #         assert xch_balance_now - xch_balance_before == xch_amount

    #         token_balance_now = await self.get_balance(wallet_client, token_tail_hash)
    #         assert token_balance_before - token_balance_now == token_amount

    #         liquidity_balance_now = await self.get_balance(wallet_client, pair_liquidity_tail_hash)
    #         assert liquidity_balance_before == liquidity_balance_before

    #         # 6. Remove remaining liquidity and call it a day
    #         # python3 tibet.py remove-liquidity --liquidity-token-amount 4200 --asset-id [asset_id] --push-tx
    #         current_pair_coin, pair_creation_spend, pair_state, sb_to_aggregate, _ = await sync_pair(
    #             full_node_client, current_pair_coin.name(), token_tail_hash
    #         )
    #         assert pair_state["liquidity"] == 4200

    #         xch_amount = pair_state["xch_reserve"]
    #         token_amount = pair_state["token_reserve"]
    #         liquidity_token_amount = pair_state["liquidity"]

    #         offer_dict = {}
    #         offer_dict[1] = xch_amount + liquidity_token_amount # also ask for xch from liquidity cat burn
    #         offer_dict[token_wallet_id] = token_amount
    #         offer_dict[liquidity_wallet_id] = -liquidity_token_amount
    #         offer_resp = await wallet_client.create_offer_for_ids(offer_dict)
    #         offer = offer_resp[0]
    #         offer_str = offer.to_bech32()

    #         xch_reserve_coin, token_reserve_coin, token_reserve_lineage_proof = await get_pair_reserve_info(
    #             full_node_client,
    #             pair_launcher_id,
    #             current_pair_coin,
    #             token_tail_hash,
    #             pair_creation_spend,
    #             sb_to_aggregate
    #         )

    #         sb = await respond_to_remove_liquidity_offer(
    #             pair_launcher_id,
    #             current_pair_coin,
    #             pair_creation_spend,
    #             token_tail_hash,
    #             pair_state["liquidity"],
    #             pair_state["xch_reserve"],
    #             pair_state["token_reserve"],
    #             offer_str,
    #             xch_reserve_coin,
    #             token_reserve_coin,
    #             token_reserve_lineage_proof
    #         )

    #         resp = await full_node_client.push_tx(sb)

    #         assert resp["success"]
    #         await self.wait_for_full_node_sync(full_node_client)

    #         xch_balance_now = await self.get_balance(wallet_client)
    #         assert xch_balance_now == xch_balance_before_all_ops

    #         token_balance_now = await self.get_balance(wallet_client, token_tail_hash)
    #         assert token_balance_now == token_total_supply

    #         liquidity_balance_now = await self.get_balance(wallet_client, pair_liquidity_tail_hash)
    #         assert liquidity_balance_now == 0
    #     finally:
    #         full_node_client.close()
    #         wallet_client.close()
    #         await full_node_client.await_closed()
    #         await wallet_client.await_closed()
