"""Coin identity verification: chain + contract address -> Gate.io currency.

Tickers are not unique (Gate.io itself lists different tokens whose names
clash), so a coin is matched to Gate.io by its contract address on a given
chain. The Gate currency code is then derived from that match, never guessed
from the ticker.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.utils import timezone

from ..exchanges.base import CurrencyInfo, ExchangeClient
from ..models import Coin, normalize_contract


@dataclass
class VerifyResult:
    ok: bool = True
    gate_currency: str = ""
    name: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    @property
    def summary(self) -> str:
        parts = self.errors or self.warnings or ["Verified on Gate.io"]
        return "; ".join(parts)[:255]


def find_by_contract(client: ExchangeClient, address: str, chain: str | None = None) -> list[tuple[CurrencyInfo, str]]:
    """All Gate.io currencies whose chain list contains this contract address.

    Returns (currency, chain_code) pairs. If `chain` is given only that chain matches.
    """
    key = normalize_contract(address)
    if not key:
        return []
    matches = []
    for cur in client.list_currencies():
        for ch in cur.chains:
            if normalize_contract(ch.contract_address) == key and (chain is None or ch.chain == chain.upper()):
                matches.append((cur, ch.chain))
    return matches


def verify_identity(
    client: ExchangeClient, *, symbol: str, chain: str, contract_address: str, gate_currency: str, quote: str
) -> VerifyResult:
    res = VerifyResult()
    chain = chain.upper()
    key = normalize_contract(contract_address)

    if key:
        matches = find_by_contract(client, key, chain)
        if not matches:
            elsewhere = find_by_contract(client, key)
            if elsewhere:
                found = ", ".join(sorted({f"{c.currency} on {ch}" for c, ch in elsewhere}))
                res.fail(f"Contract is listed on Gate.io under a different chain code: {found}.")
            else:
                res.fail(f"No Gate.io currency has contract {contract_address} on chain {chain}.")
            return res
        codes = sorted({c.currency for c, _ in matches})
        if gate_currency and gate_currency.upper() not in codes:
            res.fail(
                f"Gate.io code {gate_currency.upper()} does not own this contract; "
                f"the contract belongs to {', '.join(codes)}."
            )
            return res
        if not gate_currency and len(codes) > 1:
            res.fail(f"Contract maps to several Gate.io codes ({', '.join(codes)}); set the Gate currency explicitly.")
            return res
        cur = next(c for c, _ in matches if c.currency == (gate_currency.upper() or codes[0]))
    else:
        code = (gate_currency or symbol).upper()
        cur = client.get_currency(code)
        if cur is None:
            res.fail(f"Gate.io has no currency {code}.")
            return res
        native = [ch for ch in cur.chains if ch.chain == chain and not ch.contract_address]
        if not native:
            tokens = [ch.chain for ch in cur.chains if ch.chain == chain]
            if tokens:
                res.fail(f"{code} on {chain} is a token with a contract address; enter that address.")
            else:
                listed = ", ".join(sorted({ch.chain for ch in cur.chains})) or "none"
                res.fail(f"{code} is not native to chain {chain} on Gate.io (chains: {listed}).")
            return res

    res.gate_currency = cur.currency
    res.name = cur.name
    if cur.delisted:
        res.fail(f"{cur.currency} is delisted on Gate.io.")
    if cur.trade_disabled:
        res.warnings.append(f"Trading of {cur.currency} is currently disabled on Gate.io.")

    pair = f"{cur.currency}_{quote.upper()}"
    if cur.currency == quote.upper():
        res.fail(f"{cur.currency} is the quote currency itself; choose another quote (e.g. USDC or BTC).")
        return res
    status = client.pair_status(pair)
    if status is None:
        res.fail(f"Spot pair {pair} does not exist on Gate.io.")
    elif status != "tradable":
        res.warnings.append(f"Pair {pair} status is '{status}'.")

    if symbol and symbol.upper() != cur.currency:
        res.warnings.append(f"Your symbol {symbol.upper()} differs from Gate.io's code {cur.currency}.")
    return res


def verify_coin(client: ExchangeClient, coin: Coin, save: bool = True) -> VerifyResult:
    res = verify_identity(
        client,
        symbol=coin.symbol,
        chain=coin.chain,
        contract_address=coin.contract_address,
        gate_currency=coin.gate_currency,
        quote=coin.quote,
    )
    if save:
        if res.ok:
            coin.gate_currency = res.gate_currency
            coin.name = coin.name or res.name
            coin.verified_at = timezone.now()
        else:
            coin.verified_at = None
        coin.verification_note = res.summary
        coin.save(update_fields=["gate_currency", "name", "verified_at", "verification_note", "updated_at"])
    return res
