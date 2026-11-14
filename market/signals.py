from django.dispatch import Signal

# Sent by `stream_candles` right after a live 1m candle closes and today's levels are updated.
# kwargs: coin (market.Coin), open_time (datetime of the NEW, still-forming minute)
# Receivers run inside the stream process; keep them fast. Errors are logged, never raised.
candle_closed = Signal()

# Sent by `stream_candles` on every live price update (about every 2-4 s per coin).
# kwargs: coin (market.Coin), price (float, last traded price). Used by bots to exit at the
# stop / target between bars. Must be fast; errors are logged, never raised.
price_tick = Signal()
