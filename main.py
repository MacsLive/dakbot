# =============================================================================
# main.py — ESP32-S3 Daktronics AllSport 5000 / Colorado System 7 → JSON web server
#
# Reads serial data from a Daktronics AllSport 5000 scoreboard controller or a
# Colorado System 7 swim-timing console (selected via the "console" setting),
# parses it into a dict, and serves the current state as JSON over Ethernet.
# A web UI at /settings allows configuration without reflashing.
#
# MicroPython build requirement:
#   The standard ESP32-S3 MicroPython binary does NOT include W5500 support.
#   You need a build compiled with: MICROPY_PY_NETWORK_WIZNET5K=5500
#   Build from source:
#     make BOARD=ESP32_GENERIC_S3 MICROPY_PY_NETWORK_WIZNET5K=5500
#   Verify: import network; print(hasattr(network, 'PHY_W5500'))  # must be True
#
# Flash the following files to the device root (/):
#   main.py, config.py, settings.py, daktronics.py, colorado.py, webserver.py,
#   vmix.py, daksports.json
# =============================================================================

import ujson
import uasyncio as asyncio
from machine import SPI, Pin

import config
import settings
import webserver
from daktronics import Daktronics


# =============================================================================
# Ethernet initialisation
# =============================================================================
def init_ethernet():
    """
    Bring up the W5500 using the ESP32-port network.LAN API.

    IMPORTANT: network.WIZNET5K is the RP2040/Pico API — it does not exist on
    ESP32. The correct API is network.LAN with phy_type=network.PHY_W5500.
    The INT pin is mandatory; the driver is interrupt-driven, not polling.

    For LAN8720 RMII boards (plain ESP32, not S3) swap to:
        nic = network.LAN(mdc=Pin(23), mdio=Pin(18),
                          phy_type=network.PHY_LAN8720, phy_addr=1)
    """
    import network
    import time

    spi = SPI(
        config.SPI_ID,
        baudrate=40_000_000,
        sck=Pin(config.SPI_SCK),    # GPIO13
        mosi=Pin(config.SPI_MOSI),  # GPIO11
        miso=Pin(config.SPI_MISO),  # GPIO12
    )

    nic = network.LAN(
        spi=spi,
        phy_type=network.PHY_W5500,
        phy_addr=0,                     # always 0 for SPI-based W5500
        cs=Pin(config.W5500_CS),        # GPIO14
        int=Pin(config.W5500_INT),      # GPIO10 — required by driver
        reset=Pin(config.W5500_RST),    # GPIO9
    )

    nic.active(True)

    try:
        settings.set_mac(nic.config('mac'))
    except Exception as e:
        print('WARNING: could not read Ethernet MAC:', e)

    s = settings.current
    if s.get('use_dhcp'):
        nic.ifconfig('dhcp')
        for _ in range(30):             # wait up to 15 s for DHCP lease
            if nic.isconnected():
                break
            time.sleep(0.5)
    else:
        nic.ifconfig((
            s.get('ip',      '192.168.1.100'),
            s.get('mask',    '255.255.255.0'),
            s.get('gateway', '192.168.1.1'),
            s.get('dns',     '8.8.8.8'),
        ))

    if nic.isconnected():
        print('Ethernet connected:', nic.ifconfig())
        webserver.device_ip = nic.ifconfig()[0]
    else:
        print('WARNING: Ethernet not connected — check wiring / settings')

    return nic


# =============================================================================
# Score processing
# =============================================================================

# Shown for a field until the console has sent anything at that position —
# some consoles/codes only send part of the RTD buffer (e.g. just the main
# clock), which would otherwise leave these blank. Applied before
# apply_sport_processing(), so derived fields follow ('Game' 1 -> '1st').
_FIELD_DEFAULTS = {
    'basketball': {
        'MainClockTime':          '20:00',
        'ShotClockTime':          '30',
        'Game':                   '1',
        'HomeTeamScore':          '0',
        'AwayTeamScore':          '0',
        'HomeTeamFouls':          '0',
        'AwayTeamFouls':          '0',
        'HomeTimeOutsLeft-Total': '6',
        'AwayTimeOutsLeft-Total': '6',
    },
}


def apply_field_defaults(data, dak, sport_name):
    for key, value in _FIELD_DEFAULTS.get(sport_name, {}).items():
        if not dak.was_received(key):
            data[key] = value


def build_score_dict(dak, sport_config):
    """Extract all fields from the RTD buffer into a flat dict."""
    data = {}
    for key in sport_config:
        data[key] = dak[key].strip()
    return data


# --- Sport-specific post-processors -----------------------------------------
# Each function receives the data dict and mutates it in place.
# Add a new entry here to support additional sports.

def _process_baseball(data):
    if 'HomeAtBat' in data:
        data['HomeAtBat'] = data['HomeAtBat'].replace('<', '>')

    ball, strike = data.get('Ball', ''), data.get('Strike', '')
    data['Count'] = '{}-{}'.format(ball, strike) if ball and strike else ''

    out_str = data.get('Out', '').strip()
    data['Outs'] = (
        '\u26ab\u26ab\u26ab' if out_str == '3' else   # ⚫⚫⚫
        '\u26ab\u26ab\u26aa' if out_str == '2' else   # ⚫⚫⚪
        '\u26ab\u26aa\u26aa' if out_str == '1' else   # ⚫⚪⚪
        '\u26aa\u26aa\u26aa' if out_str == '0' else   # ⚪⚪⚪
        ''
    )

    inning_state = data.get('InningDescription', '').lower()
    tb = (
        '\u25bc' if 'bot' in inning_state else
        '\u25b2' if 'top' in inning_state else
        ''
    )
    data['TB']     = tb
    data['top']    = '\u25b2' if tb == '\u25b2' else ''
    data['bottom'] = '\u25bc' if tb == '\u25bc' else ''

def _timeout_display(count_str):
    """2 -> '——'; blank/non-numeric -> ''."""
    try:
        n = int(count_str)
    except (ValueError, TypeError):
        return ''
    return '—' * n


def _process_football(data):
    down, togo = data.get('Down', ''), data.get('ToGo', '')
    data['DownAndDistance'] = '{} & {}'.format(down, togo) if down and togo else ''

    data['HomeTimeoutDisplay'] = _timeout_display(data.get('HomeTimeOutsLeft-Total', ''))
    data['AwayTimeoutDisplay'] = _timeout_display(data.get('AwayTimeOutsLeft-Total', ''))

    play_clock = data.get('PlayClockTime', '')
    try:
        under_10 = int(play_clock) < 10
    except (ValueError, TypeError):
        under_10 = False
    data['PlayClockTimeAlert'] = play_clock if under_10 else ''


_ORDINALS = ('1st', '2nd', '3rd', '4th')


def _period_text(period_str, regulation):
    """Period number -> '1st'.. for regulation, then 'OT', 'OT2', 'OT3'...
    regulation is 2 (college halves) or 4 (high school quarters)."""
    s = period_str.strip()
    try:
        n = int(s)
    except (ValueError, TypeError):
        return s            # console sent text (or blank) — pass it through
    if n <= 0:
        return ''
    if n <= regulation:
        return _ORDINALS[n - 1]
    ot = n - regulation
    return 'OT' if ot == 1 else 'OT{}'.format(ot)


def _process_basketball(data):
    # 'Game' is the AllSport basketball period number (item 142)
    regulation = 4 if settings.current.get('basketball_level') == 'highschool' else 2
    data['GameHalfText'] = _period_text(data.get('Game', ''), regulation)


_SPORT_PROCESSORS = {
    'baseball':   _process_baseball,
    'basketball': _process_basketball,
    'football':   _process_football,
}


# Secondary clocks (8-char field at 201) — at 0 the console fills the field
# with a horn/expired marker alongside the time (e.g. "0 z"), which would
# otherwise show on air and break PlayClockTimeAlert's int() parse.
_SECONDARY_CLOCKS = ('ShotClockTime', 'PlayClockTime')


def _clean_clock(value):
    """Keep only the time characters: '0 z' -> '0', '0:24' -> '0:24'."""
    return ''.join(c for c in value if c.isdigit() or c in ':.')


# Every Daktronics clock field — tenths are only shown under 10 seconds
_CLOCK_FIELDS = (
    'MainClockTime', 'MainClockTimeAlt',
    'MainClockTimeOutTOD', 'MainClockTimeOutTODAlt',
    'ShotClockTime', 'PlayClockTime', 'TimeOutTime',
)


def _trim_tenths(value):
    """Drop the fractional part unless under 10 s: '45.3' -> '45',
    '1:05.3' -> '1:05', '9.8' -> '9.8'. Truncates (not rounds) so a
    countdown reads 1:00 -> 59 ... 10 -> 9.9. Non-clock text is untouched."""
    if '.' not in value:
        return value
    whole = value.split('.', 1)[0]
    secs = 0
    try:
        for part in whole.split(':'):
            secs = secs * 60 + (int(part) if part else 0)
    except ValueError:
        return value
    return whole if secs >= 10 else value


def apply_sport_processing(data, sport_name):
    for key in _SECONDARY_CLOCKS:
        if key in data:
            data[key] = _clean_clock(data[key])
    for key in _CLOCK_FIELDS:
        if key in data:
            data[key] = _trim_tenths(data[key])

    processor = _SPORT_PROCESSORS.get(sport_name)
    if processor:
        processor(data)


# =============================================================================
# Async tasks
# =============================================================================
# Fields excluded from the "keep last known non-empty value" fallback below —
# these legitimately go blank as a normal state transition (not a serial
# glitch), so sticking them would make them permanently latch to their first
# non-blank value.
_STICKY_EXCLUDED = (
    'AwayAtBat', 'HomeAtBat',
    'DownAndDistance', 'HomeTimeoutDisplay', 'AwayTimeoutDisplay',
    'PlayClockTimeAlert',
)


async def serial_reader_task(dak, sport_config, sport_name, mqtt_enabled=False,
                             vmix_enabled=False):
    """Read RTD packets continuously, yield between bytes for HTTP responsiveness."""
    print('Serial reader started')
    _mqtt_enabled = mqtt_enabled
    if vmix_enabled:
        import vmix
    while True:
        try:
            await dak.update()
            new_data = build_score_dict(dak, sport_config)
            apply_field_defaults(new_data, dak, sport_name)
            apply_sport_processing(new_data, sport_name)

            # Keep last known non-empty value for fields that report blank mid-packet
            for key, val in new_data.items():
                if key not in _STICKY_EXCLUDED:
                    if val == '' and key in webserver.score_data:
                        new_data[key] = webserver.score_data[key]

            new_data['sport'] = sport_name
            new_data['console'] = 'daktronics'
            webserver.score_data.update(new_data)

            # Wake vMix first — the clock is the latency-sensitive one.
            # vmix.run() only sends the mapped fields that changed.
            if vmix_enabled:
                vmix.data_ready.set()

            # Signal MQTT publisher that fresh data is available
            if _mqtt_enabled:
                import mqtt_publisher
                mqtt_publisher.data_ready.set()

        except Exception as e:
            print('Serial reader error:', e)
            await asyncio.sleep_ms(500)


async def colorado_reader_task(cw, mqtt_enabled=False, vmix_enabled=False):
    """Read Colorado System 7 data continuously. update() only returns once
    a meaningful field has changed (see colorado.py)."""
    print('Colorado reader started')
    while True:
        try:
            await cw.update()
            new_data = cw.to_dict()
            new_data['console'] = 'colorado'
            webserver.score_data.update(new_data)

            # Wake vMix first — the clock is the latency-sensitive one.
            # vmix.run() only sends the mapped fields that changed.
            if vmix_enabled:
                import vmix
                vmix.data_ready.set()

            if mqtt_enabled:
                import mqtt_publisher
                mqtt_publisher.data_ready.set()

        except Exception as e:
            print('Colorado reader error:', e)
            await asyncio.sleep_ms(500)


# =============================================================================
# Entry point
# =============================================================================
async def main():
    # 1. Ethernet (reads network settings from settings.current)
    webserver.nic = init_ethernet()

    mqtt_enabled = bool(settings.current.get('mqtt_enabled'))
    console_type = settings.current.get('console', 'daktronics')
    # Always runs — vmix.run() idles while disabled in settings, so turning
    # it on in /settings doesn't need a reboot
    vmix_enabled = True

    # 2. Initialise the selected console's serial parser
    if console_type == 'colorado':
        from colorado import Colorado
        cw = Colorado(
            uart_id=config.UART_ID,
            rx_pin=settings.current['uart_rx'],
            tx_pin=settings.current.get('uart_tx', 17),
            baud=config.COLORADO_BAUD,
        )
        print('Console: Colorado System 7')

        # Seed score_data so the endpoint always returns valid JSON before first update
        webserver.score_data = cw.to_dict()
        webserver.score_data['console']     = 'colorado'
        webserver.score_data['device_name'] = settings.device_name()

        reader = colorado_reader_task(cw, mqtt_enabled, vmix_enabled)

    else:
        with open('daksports.json') as f:
            all_sports = ujson.load(f)

        sport_name   = settings.current['sport']
        sport_config = all_sports[sport_name]
        print('Sport:', sport_name, ' | RTD buffer:', sport_config['dakSize'][1], 'chars')

        dak = Daktronics(
            sport_config,
            uart_id=config.UART_ID,
            rx_pin=settings.current['uart_rx'],
            tx_pin=settings.current.get('uart_tx', 17),
            baud=config.UART_BAUD,
        )

        # Seed score_data so the endpoint always returns valid JSON before first packet
        webserver.score_data = {key: '' for key in sport_config}
        apply_field_defaults(webserver.score_data, dak, sport_name)
        apply_sport_processing(webserver.score_data, sport_name)
        webserver.score_data['sport']       = sport_name
        webserver.score_data['console']     = 'daktronics'
        webserver.score_data['device_name'] = settings.device_name()

        reader = serial_reader_task(dak, sport_config, sport_name, mqtt_enabled,
                                    vmix_enabled)

    # 3. Run HTTP server and serial reader concurrently
    await webserver.start(port=settings.current.get('http_port', 80))

    tasks = [asyncio.create_task(reader)]

    # 4. Optionally start MQTT publisher
    if mqtt_enabled:
        import mqtt_publisher
        tasks.append(asyncio.create_task(
            mqtt_publisher.run(lambda: webserver.score_data)
        ))
        print('MQTT publisher task started')

    # 5. Push the clock straight to vMix (idles until enabled in /settings)
    if vmix_enabled:
        import vmix
        tasks.append(asyncio.create_task(vmix.run()))
        print('vMix pusher task started')

    await asyncio.gather(*tasks)


asyncio.run(main())
