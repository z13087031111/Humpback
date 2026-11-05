"""Venue adapters.

Each venue is split into a pure *codec* (capabilities, precision rules, client-id encoding,
request building) and a *transport* (network I/O). Codecs are fully testable offline;
transports are the only code that talks to an exchange.
"""
