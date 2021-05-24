from __future__ import annotations
import logging
import asyncio
import os
import shutil
import binascii
import quart_cors
from typing import Optional, Dict, List, Tuple
from datetime import datetime
from random import randint
from neo3 import blockchain, settings, vm
from neo3.network import payloads, convenience
from neo3.storage import implementations
from neo3.core import msgrouter, cryptography, types
from quart import Quart

STORAGE_PATH = '/tmp/neo3/'

stdio_handler = logging.StreamHandler()
stdio_handler.setLevel(logging.DEBUG)
stdio_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s - %(module)s:%(lineno)s %(message)s"))

network_logger = logging.getLogger('neo3.network')
network_logger.addHandler(stdio_handler)
network_logger.setLevel(logging.DEBUG)

settings.network.seedlist = [os.getenv('VOTING_NETWORK')]
settings.network.magic = 423547407
settings.network.standby_committee = [
    "0378fd176ffa7f09780402e7258938aeb37d140b00eac9a4620885bae06833422a",
    "03e1fd6232b305b289c249f9ec4a5ac92049c9d09daf3917ace11562a05af757aa",
    "027f4d2333d6188c394f2c706bd4e5567754c6b84174e404cc7e07cc2c0815103a",
    "035c73b00dbd728b570e46c0e3af72d646cd8c057e2a4f6a8f27439763b5beaabc"]
settings.network.validators_count = 4

chain = blockchain.Blockchain(implementations.LevelDB({'path': STORAGE_PATH}))


async def sync_from_network():
    seedlist = settings.network.seedlist
    if len(seedlist) == 0 or seedlist[1] is None:
        print("Must set 'VOTING_NETWORK' evironment variable to valid P2P network address")
        SystemExit(-1)
    node_mgr = convenience.NodeManager()
    node_mgr.start()

    sync_mgr = convenience.SyncManager()
    await sync_mgr.start()


def sync_from_file():
    shutil.rmtree(STORAGE_PATH)
    with open('chain.1.acc', 'rb') as f:
        unknown = int.from_bytes(f.read(4), 'little')
        total_blocks_size = int.from_bytes(f.read(4), 'little')
        start = datetime.now()
        for i, _ in enumerate(range(total_blocks_size)):
            size = int.from_bytes(f.read(4), 'little')
            b = payloads.Block.deserialize_from_bytes(f.read(size))
            # if b.index <= chain.currentSnapshot.best_block_height:
            #     continue
            if b.index == 1395:
                break
            chain.persist(b)
        cur = datetime.now()
        delta = cur - start
        print(delta)


# Chain event processing code
class DB:
    def __init__(self):
        self._candidates: List[Candidate] = []
        self.current_committee: Dict[cryptography.ECPoint, vm.BigInteger] = {}
        # total candidate registrations
        self.candidate_count_y = [0]
        self.candidate_blockindex_x = [0]

        self.total_votes = [0]
        self.total_votes_blockindex = [0]

    @property
    def candidates(self) -> List[Candidate]:
        # this auto sorts by votes
        try:
            self._candidates.sort(reverse=True)
        except Exception as e:
            pass
        return self._candidates

    @property
    def vote_history(self) -> List[Tuple[int, int]]:
        """
        Return a list of block_height, total_votes tuples, where total_votes is at that point in time
        """
        return list(zip(self.total_votes_blockindex, self.total_votes))

    @property
    def total_candidates(self) -> int:
        return self.candidate_count_y[-1]

    def get_candidate(self, public_key: cryptography.ECPoint) -> Candidate:
        for c in self._candidates:
            if c.public_key == public_key:
                return c
        else:
            raise ValueError("Can't find candidate")

    def register_candidate(self, public_key: cryptography.ECPoint, block: payloads.Block):
        self._candidates.append(Candidate(public_key, block))

        # first candidate registration in the new block
        if block.index not in self.candidate_blockindex_x:
            self.candidate_blockindex_x.append(block.index)
        self.candidate_count_y.append(self.candidate_count_y[-1] + 1)

    def unregister_candidate(self, public_key: cryptography.ECPoint, block_index: int):
        self._candidates.remove(Candidate(public_key))
        if block_index not in self.candidate_blockindex_x:
            self.candidate_blockindex_x.append(block_index)
        self.candidate_count_y.append(self.candidate_count_y[-1] - 1)

    def add_vote(self, for_candidate: cryptography.ECPoint, amount: int, casted_by: types.UInt160, block_index: int):
        self.get_candidate(for_candidate).add_vote(amount, casted_by)
        if block_index not in self.total_votes_blockindex:
            self.total_votes_blockindex.append(block_index)
        self.total_votes.append(self.total_votes[-1] + amount)

    def remove_vote(self, for_candidate: cryptography.ECPoint, amount: int, casted_by: types.UInt160, block_index: int):
        self.get_candidate(for_candidate).remove_vote(amount, casted_by)
        if block_index not in self.total_votes_blockindex:
            self.total_votes_blockindex.append(block_index)
        self.total_votes.append(self.total_votes[-1] - amount)


class Candidate:
    def __init__(self, public_key, block: Optional[payloads.Block] = None):
        self.public_key: cryptography.ECPoint = public_key
        self.registration_date = datetime.utcfromtimestamp(block.header.timestamp / 1000) if block else datetime.now()
        self.votes = 0
        self.voted_accounts: Dict[types.UInt160, int] = {}
        self.registration_height = block.index if block.index else 0

    def __eq__(self, other):
        if type(self) == type(other) and self.public_key == other.public_key:
            return True
        else:
            return False

    def __lt__(self, other):
        return self.votes < other.votes

    def __repr__(self):
        return f"<{self.__class__.__name__} @ {hex(id(self))}> {str(self.public_key)[:6]}.. votes:{self.votes}"

    @property
    def unique_voters(self) -> int:
        return len(self.voted_accounts)

    def add_vote(self, amount: int, casted_by: types.UInt160) -> None:
        self.votes += amount
        if casted_by in self.voted_accounts:
            self.voted_accounts[casted_by] += amount
        else:
            self.voted_accounts.update({casted_by: amount})

    def remove_vote(self, amount: int, casted_by: types.UInt160) -> bool:
        self.votes -= amount
        if casted_by not in self.voted_accounts:
            return False

        self.voted_accounts[casted_by] -= amount
        if self.voted_accounts[casted_by] == 0:
            self.voted_accounts.pop(casted_by)
        return True

    def to_json(self):
        return {
            "public_key": self.public_key,
            "registration_data": self.registration_date,
            "votes": self.votes,
            "unique_voters": self.unique_voters,
        }


db = DB()


def vote(account, balance, new_vote_candidate: cryptography.ECPoint, old_vote_candidate: Optional[cryptography.ECPoint],
         block: payloads.Block):
    if new_vote_candidate is None and old_vote_candidate is not None:
        print(f"[{block.index}] Delete vote by account: {account} from candidate: {old_vote_candidate}")
        db.remove_vote(old_vote_candidate, balance, account, block.index)

    elif new_vote_candidate is not None and old_vote_candidate is None:
        print(f"[{block.index}] New vote by account: {account} for candidate: {new_vote_candidate} votes: {balance}")
        db.add_vote(new_vote_candidate, balance, account, block.index)

    elif new_vote_candidate and old_vote_candidate:
        print(
            f"[{block.index}] Change vote by account: {account} from candidate: {old_vote_candidate} to candidate: {new_vote_candidate}")
        db.remove_vote(old_vote_candidate, balance, account, block.index)
        db.add_vote(new_vote_candidate, balance, account, block.index)


def register_candidate(public_key: cryptography.ECPoint, block: payloads.Block):
    print(f"[{block.index}] New candidate registration: {public_key}")
    db.register_candidate(public_key, block)


def unregister_candidate(public_key: cryptography.ECPoint, block: payloads.Block):
    print(f"[{block.index}] Remove candidate registration: {public_key}")
    db.unregister_candidate(public_key, block.index)


def neo_balance_changing(account: types.UInt160, candidate_public_key: cryptography.ECPoint, balance: vm.BigInteger,
                         block: payloads.Block):
    # amount can be negative too
    # changing neo balance means changing votes
    # we should track user balance such that we can track if they've sent their whole fund, which means losing a unique voter
    print(f"[{block.index}] Change vote by account {account} for candidate: {candidate_public_key} votes: {balance}")


def new_committee(validators: Dict[cryptography.ECPoint, vm.BigInteger], block: payloads.Block):
    committee_changed = False
    for validator in validators:
        if validator not in db.current_committee:
            committee_changed = True
            db.current_committee = validators
            break
    if committee_changed:
        print("New committee")
        print("-" * 20)
        for k, v in validators.items():
            print(f"[{block.index}] {str(k), str(v)}")


msgrouter.vote += vote
msgrouter.register_candidate += register_candidate
msgrouter.unregister_candidate += unregister_candidate
msgrouter.neo_balance_changing += neo_balance_changing
msgrouter.new_committee += new_committee

# Webserver code
app = Quart(__name__)
app = quart_cors.cors(app, allow_origin="*")


@app.route('/votes')
async def total_votes():
    return {
        "threshold": 20_000_000,
        "block_index": db.total_votes_blockindex,
        "vote_count": db.total_votes
    }


@app.route('/registered_candidates')
async def candidates():
    candidates = []
    for i, c in enumerate(db.candidates):
        candidates.append({"rank": i + 1, "public_key": str(c.public_key), "votes": c.votes})

    return {
        "block_index": db.candidate_blockindex_x,
        "candidate_count": db.candidate_count_y,
        "candidates": candidates
    }


@app.route('/committee')
async def committee():
    committee = []
    for key, votes in db.current_committee.items():
        committee.append({str(key): int(votes)})
    return {"committee": committee}


@app.route("/candidate/<publickey>")
async def candidate(publickey):
    ecpoint = cryptography.ECPoint.deserialize_from_bytes(bytes.fromhex(publickey))
    c = db.get_candidate(ecpoint)
    candidates = db.candidates
    position = candidates.index(c) + 1
    total_votes = db.total_votes[-1]
    committee_size = len(db.current_committee)

    return {
        "public_key": publickey,
        "registration_date": c.registration_date,
        "votes": c.votes,
        "unique_voters": c.unique_voters,
        "in_committee": True if total_votes >= 20_000_000 and position <= committee_size else False,
        "position": position,
        "total_candidates": len(candidates)
    }


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    sync_from_file()
    # loop.create_task(sync_from_network())
    app.run(loop=loop, debug=True)
