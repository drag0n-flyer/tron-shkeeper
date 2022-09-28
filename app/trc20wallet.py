import concurrent
from copy import copy
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Literal

import tronpy.exceptions
from tronpy.keys import PrivateKey

from .config import config, get_contract_address
from .db import query_db2
from .logging import logger
from .utils import get_tron_client


@dataclass
class Account:
    addr: str  # public key
    tokens: Decimal = 0
    currency: Decimal = 0
    bandwidth: int = 0
    bandwidth_limit: int = 1500

    @property
    def private_key(self):
        return query_db2('select * from keys where type = "onetime" and public = ?', (self.addr,), one=True)['private']


class Trc20Wallet:

    def __init__(self, symbol):
        self.symbol = symbol
        self.client = get_tron_client()
        self.contract = self.client.get_contract(get_contract_address(symbol))
        self.precision = self.contract.functions.decimals()
        self.accounts = self.refresh_accounts()

    def refresh_accounts(self) -> List[Account]:
        public_keys = [row['public'] for row in query_db2('select public from keys where symbol = ? and type = "onetime"', (self.symbol, ))]

        # public_keys = NILE_ACCS[-200:]

        def get(addr) -> Account:
            retries = 0
            while retries < config['CONCURRENT_MAX_RETRIES']:
                try:
                    tokens = Decimal(self.contract.functions.balanceOf(addr)) / 10 ** self.precision
                    try:
                        currency = self.client.get_account_balance(addr)
                        bandwidth = Account.bandwidth_limit - self.client.get_account_resource(addr).get('freeNetUsed', 0)
                    except tronpy.exceptions.AddressNotFound:
                        currency = Decimal(0)
                        bandwidth = 0
                    logger.info(f"{addr} -> {tokens}")
                    return Account(addr=addr, tokens=tokens, currency=currency, bandwidth=bandwidth)
                except tronpy.exceptions.UnknownError as e:
                    logger.exception(f'Exception during {addr} refresh: {e}')
                    retries += 1
            raise Exception(f'CONCURRENT_MAX_RETRIES exeeded while processing {addr}')

        start = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=config['CONCURRENT_MAX_WORKERS']) as executor:
            accounts = list(executor.map(get, public_keys))
            accounts.sort(key=lambda account: account.tokens, reverse=True)
            self.last_refresh_duration = time.time() - start
            return accounts

    @property
    def tokens(self):
        return sum([account.tokens for account in self.accounts])

    @property
    def accounts_with_tokens(self):
        return list(filter(lambda acc: acc.tokens > 0, self.accounts))

    @property
    def accounts_without_tokens(self):
        return list(filter(lambda acc: acc.tokens == 0, self.accounts))

    @property
    def accounts_with_bandwidth(self):
        return list(filter(lambda acc: acc.bandwidth > 0, self.accounts))

    @property
    def accounts_with_currency(self):
        return list(filter(lambda acc: acc.currency > 0, self.accounts))


class PayoutStrategy:

    def __init__(self, wallet: Trc20Wallet, payout_list: list):
        self.wallet = wallet
        self.payout_list = payout_list
        self.check_payout_list()
        self.steps = []

    def check_payout_list(self):
        payout_total = sum([payout['amount'] for payout in self.payout_list])
        if not payout_total:
            raise Exception('Payout amount can not be 0')
        if payout_total > self.wallet.tokens:
            raise Exception(f'Not enough tokens to complete payout. Need: {payout_total}, has: {self.wallet.tokens}')

    def generate_steps(self):
        for i, payout in enumerate(self.payout_list, 1):
            logger.info(f"Step {i}")
            logger.info(f"Wallet token balance: {self.wallet.tokens}")
            logger.info(self.wallet.accounts_with_tokens)
            transfer_list = self.step(payout['dest'], payout['amount'])
            logger.info(f"Transfer list: {transfer_list}")

        logger.info(f"Number of transfers: %r", len([transfer for transfers in self.steps
                                                     for transfer in transfers]))
        logger.info(f'Estimated fee: %r', self.estimate_fee())
        logger.info(f"Requested payout amount: %r", sum([payout['amount'] for payout in self.payout_list]))
        logger.info(f"Collected payout amount: %r", sum([transfer['amount'] for transfers in self.steps
                                                                   for transfer in transfers]))
        logger.info(f"Final wallet token balance: {self.wallet.tokens}")
        return self.steps

    def estimate_fee(self):
        if not self.steps:
            self.generate_steps()

        accounts_num = len([transfer for transfers in self.steps
                                     for transfer in transfers])
        activation_and_transfer_fee = 2
        fee = accounts_num * (config['TX_FEE'] + activation_and_transfer_fee)
        return {
            'accounts_num': accounts_num,
            'fee': fee,
        }

    def seed_payout_fees(self):
        fee_deposit_key = query_db2('select * from keys where type = "fee_deposit" ', one=True)
        priv_key = PrivateKey(bytes.fromhex(fee_deposit_key['private']))

        fee_deposit_account_balance = self.wallet.client.get_account_balance(fee_deposit_key['public'])
        accounts_need_seeding = [transfer['src'] for transfers in self.steps
                                                 for transfer in transfers
                                                    if transfer['src'].currency < config['TX_FEE']]

        need_currency = len(accounts_need_seeding) * config['TX_FEE']
        if fee_deposit_account_balance < need_currency:
            raise Exception(f'Fee deposit account has not enought currency. Has: {fee_deposit_account_balance} need: {need_currency}')

        def seed(acc: Account):
            try:
                amount_to_seed = config['TX_FEE'] - acc.currency
                txn = (
                    self.wallet.client.trx.transfer(fee_deposit_key['public'], acc.addr, int(amount_to_seed * 1_000_000))
                    .build()
                    .sign(priv_key)
                )
                txn.broadcast().wait()
                logger.info(f"Seed {amount_to_seed} TRX -> {acc.addr} | {txn.txid}")
                return {'addr': acc, 'txid': txn.txid}
            except Exception as e:
                logger.exception(f"Exception while seeding {amount_to_seed} TRX -> {acc.addr}: {e}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=config['CONCURRENT_MAX_WORKERS']) as executor:
            return list(executor.map(seed, accounts_need_seeding))


    def step(self, dst, amount):
        if amount <= 0:
            raise Exception(f'Invalid amount porivded for payout to {dst}: {amount}')

        for acc in self.wallet.accounts_with_tokens:
            if acc.tokens == amount:
                transfers = [{'src': copy(acc), 'dst': dst, 'amount': amount, 'debug': '1 to 1'}]
                acc.tokens -= amount
                self.steps.append(transfers)
                return transfers

        if amount == self.wallet.tokens:
            transfers = []
            for acc in self.wallet.accounts_with_tokens:
                transfers.append({'src': copy(acc), 'dst': dst, 'amount': acc.tokens, 'debug': 'all to 1'})
                acc.tokens = 0
            self.steps.append(transfers)
            return transfers

        transfers = []
        collected_amount = 0
        for acc in self.wallet.accounts_with_tokens:
            if collected_amount == amount:
                self.steps.append(transfers)
                return transfers
            else:
                if collected_amount < amount:
                    to_collect = amount - collected_amount
                    if acc.tokens > to_collect:  # account has more tokens than we need to collect
                        transfers.append({'src': copy(acc), 'dst': dst, 'amount': to_collect, 'debug': 'acc partial'})
                        collected_amount += to_collect
                        acc.tokens -= to_collect
                    else:
                        transfers.append({'src': copy(acc), 'dst': dst, 'amount': acc.tokens, 'debug': 'acc full'})
                        collected_amount += acc.tokens
                        acc.tokens = 0
                else:
                    raise Exception(f'Collected too much! This should not happen!'
                                    f'Requested ammount: {amount}, collected amount: {collected_amount}, transfers list: {transfers}')

        if collected_amount == amount:
            self.steps.append(transfers)
            return transfers
        else:
            raise Exception(f'Out of accounts while collecting payout amount! This should not happen! '
                            f'Requested ammount: {amount}, collected amount: {collected_amount}, '
                            f'transfer list: {transfers}, accounts with tokens: {self.wallet.accounts_with_tokens}')