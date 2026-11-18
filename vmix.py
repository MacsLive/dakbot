# =============================================================================
# vmix.py — Push the running clock (and any other fields) straight into a
# vMix title
#
# vMix's own Data Sources poll the JSON endpoint on a timer, which puts the
# on-air clock up to a second (or more) behind the console. This instead
# holds one persistent connection to vMix's TCP API (default port 8099) and
# sends a SetText the moment a mapped field changes:
#
#   FUNCTION SetText Input=<input>&SelectedName=<field>&Value=<value>\r\n
#
# What to send comes from the 'vmix_fields' setting: "[Title name]" lines
# pick the vMix input, and each "Key = Field" line below one maps a
# score_data key to a text field in that title. Clock lines (Colorado:
# Clock, Daktronics: MainClockTime) are always sent first.
#
# A persistent TCP socket avoids the per-request connect/HTTP overhead of
# the HTTP API (port 8088), so each update is one small write.
#
# Only the latest value matters — if several changes land while a write is
# in flight, the next send just carries the newest values.
# =============================================================================

import utime
import uasyncio as asyncio
import settings as _settings
import webserver as _webserver

# Set by the console's reader task in main.py after every score_data update.
# Each wake only sends the mapped fields whose value actually changed.
data_ready = asyncio.Event()

# Everything is re-sent this often so a vMix restart or a re-added title
# picks the values back up even while nothing is changing (clock stopped)
_RESEND_MS = 5000


# score_data keys of the running clocks — sent ahead of everything else
_CLOCK_KEYS = ('MainClockTime', 'Clock')

# Shown on the /settings dashboard — the pusher has no other visible output
# short of a serial REPL, and vMix reports a bad input/field name only in
# its reply line (e.g. "FUNCTION ER Input not found").
status     = 'starting'
last_sent  = ''
last_reply = ''


def _quote(s):
    """Percent-encode a query-string value (input names may contain spaces,
    '&', etc.)."""
    out = []
    for b in str(s).encode('utf-8'):
        c = chr(b)
        if c.isalpha() or c.isdigit() or c in '-_.~:':
            out.append(c)
        else:
            out.append('%{:02X}'.format(b))
    return ''.join(out)


async def _drain(reader):
    """Consume vMix's 'FUNCTION OK ...' replies so its side never blocks on
    a full socket, keeping the latest for the dashboard, and return once the
    connection drops."""
    global last_reply
    while True:
        line = await reader.readline()
        if not line:
            return
        last_reply = line.decode().strip()


# Covers connect + handshake. MicroPython's open_connection() has no timeout
# of its own, so an unreachable host would otherwise sit at 'connecting'.
_CONNECT_TIMEOUT_S = 5


async def _connect(host, port):
    """Open the TCP API connection and prove vMix is really on the other end.

    MicroPython's open_connection() returns as soon as the socket is
    writable without checking that the connect succeeded, so a refused or
    reset connection looks 'connected' until the first read. Asking for
    VERSION and waiting for vMix's 'VERSION OK <ver>' reply turns that into
    a clear error here. Returns (reader, writer, 'vMix <ver>')."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(b'VERSION\r\n')
        await writer.drain()
        line = (await reader.readline()).decode().strip()
        if not line:
            raise OSError('connection refused or closed — is vMix running, '
                          'and is TCP {} allowed through its firewall?'.format(port))
        if not line.startswith('VERSION'):
            raise OSError('not a vMix TCP API (replied {!r})'.format(line[:40]))
        parts = line.split()
        return reader, writer, 'vMix ' + (parts[2] if len(parts) > 2 else '?')
    except BaseException:
        try:
            writer.close()
        except Exception:
            pass
        raise


def parse_fields(text):
    """Parse the 'vmix_fields' setting. A '[Title name]' line picks the
    vMix input for the 'DakbotKey = vMixField' lines below it. Blank lines,
    lines without '=', and lines starting with '#' are ignored.
    Returns [(input or None, key, field), ...] — input is None for a line
    that comes before any [Title] header, which can't be sent anywhere."""
    out = []
    inp = None
    for line in (text or '').replace('\r', '\n').split('\n'):
        line = line.strip()
        if line.startswith('[') and line.endswith(']'):
            inp = line[1:-1].strip() or None
            continue
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, field = line.split('=', 1)
        key, field = key.strip(), field.strip()
        if key and field:
            out.append((inp, key, field))
    return out


# _config() runs on every wake (i.e. every serial packet), so the parsed
# result is cached against the raw settings it was built from
_cfg_raw = None
_cfg     = None


def _config():
    global _cfg_raw, _cfg
    s = _settings.current
    raw = (
        s.get('vmix_enabled'), s.get('vmix_host', ''), s.get('vmix_port', 8099),
        s.get('vmix_fields', ''),
    )
    if raw == _cfg_raw:
        return _cfg
    _cfg_raw = raw

    if not (s.get('vmix_enabled') and s.get('vmix_host')):
        _cfg = None
        return None
    # A pasted "host:8088" (the HTTP API port) would otherwise be handed to
    # open_connection as the hostname itself
    host   = s.get('vmix_host', '').split(':')[0]
    port   = int(s.get('vmix_port', 8099))

    parsed = parse_fields(s.get('vmix_fields', ''))
    fields = [f for f in parsed if f[0]]
    # Clock lines first so they're always at the front of each write
    fields = ([f for f in fields if f[1] in _CLOCK_KEYS]
              + [f for f in fields if f[1] not in _CLOCK_KEYS])

    # Pre-build each line's command up to '&Value=' — (key, prefix) pairs
    _cfg = (host, port, tuple(
        (key, 'FUNCTION SetText Input={}&SelectedName={}&Value='.format(
            _quote(inp), _quote(field)))
        for inp, key, field in fields
    ), len(parsed) - len(fields))
    return _cfg


async def run():
    """Runs for the life of the device. Settings are re-read on
    every pass, so enabling/disabling or changing vMix settings in /settings
    takes effect within a few seconds, without a reboot."""
    global status, last_sent

    while True:
        cfg = _config()
        if cfg is None:
            status = 'disabled'
            await asyncio.sleep(2)
            continue
        host, port, fields, unassigned = cfg
        if not fields:
            status = 'no fields to send — add a [Title name] line and fields below it'
            await asyncio.sleep(2)
            continue

        status = 'connecting to {}:{}'.format(host, port)
        try:
            reader, writer, version = await asyncio.wait_for(
                _connect(host, port), _CONNECT_TIMEOUT_S)
            print('vMix connected:', version)
        except asyncio.TimeoutError:
            print('vMix connect timed out')
            status = ('connect to {}:{} timed out — check the vMix PC\'s IP, that it\'s '
                      'on this network, and that its firewall allows TCP {}'
                      ).format(host, port, port)
            await asyncio.sleep(5)
            continue
        except Exception as e:
            print('vMix connect failed:', e)
            status = 'connect to {}:{} failed: {!r}'.format(host, port, e)
            await asyncio.sleep(5)
            continue

        status     = 'connected to {}:{} ({})'.format(host, port, version)
        if unassigned:
            status += ' — {} line(s) ignored: no [Title name] above them'.format(unassigned)
        drain_task = asyncio.create_task(_drain(reader))
        sent       = {}                 # command prefix -> last value sent
        last_full  = utime.ticks_ms()
        try:
            while True:
                # Time out every second so the resend timer and settings
                # check still run while no data is arriving
                try:
                    await asyncio.wait_for(data_ready.wait(), 1)
                    data_ready.clear()
                except asyncio.TimeoutError:
                    pass

                if drain_task.done():
                    raise OSError('connection closed by vMix')
                if _config() != cfg:
                    raise OSError('settings changed, reconnecting')

                now = utime.ticks_ms()
                if utime.ticks_diff(now, last_full) >= _RESEND_MS:
                    sent      = {}
                    last_full = now

                data = _webserver.score_data
                out  = []
                for key, prefix in fields:
                    value = str(data.get(key, ''))
                    if sent.get(prefix) != value:
                        out.append(prefix + _quote(value) + '\r\n')
                        sent[prefix] = value
                        last_sent = '{} = {}'.format(key, value)
                if out:
                    writer.write(''.join(out).encode())
                    await writer.drain()

        except Exception as e:
            print('vMix:', e)
            status = 'error: {!r}'.format(e)
            drain_task.cancel()
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            await asyncio.sleep(1)
