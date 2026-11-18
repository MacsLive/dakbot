# =============================================================================
# dak_check.py — Run ON THE BOARD to check whether AllSport 5000 RTD data is
# arriving on the serial input, independent of the web page, network and vMix.
#
#   mpremote connect /dev/ttyACM0 run tools/dak_check.py
#   mpremote connect /dev/ttyACM0 reset      # restart the firmware afterwards
#
# 'run' stops the firmware and runs this from your computer (nothing is
# copied to flash). It listens for SECONDS and prints the raw bytes, the
# first decoded packets, and a verdict.
# =============================================================================

import utime
import settings
import config
from machine import UART, Pin

SECONDS   = 10
SHOW_PKTS = 15

rx = settings.current['uart_rx']
u = UART(config.UART_ID, baudrate=config.UART_BAUD, rx=Pin(rx),
         tx=Pin(settings.current.get('uart_tx', 17)),
         bits=8, parity=None, stop=1, timeout=0, rxbuf=4096)
u.read()    # drop anything buffered before we started

print('Listening on UART{} RX=GPIO{} at {} baud for {} s — sport setting: {}'.format(
    config.UART_ID, rx, config.UART_BAUD, SECONDS, settings.current.get('sport')))

total, pkts, shown = 0, 0, 0
first = b''
buf = b''
end = utime.ticks_add(utime.ticks_ms(), SECONDS * 1000)

while utime.ticks_diff(end, utime.ticks_ms()) > 0:
    n = u.any()
    if not n:
        utime.sleep_ms(5)
        continue
    chunk = u.read(n)
    if not chunk:
        continue
    total += len(chunk)
    if len(first) < 64:
        first += chunk[:64 - len(first)]
    buf += chunk
    # Packets: SYN(0x16) ... SOH(0x01) code STX(0x02) text EOT(0x04) chk ETX(0x17)
    while b'\x17' in buf:
        pkt, _, buf = buf.partition(b'\x17')
        if b'\x16' not in pkt:
            continue
        pkts += 1
        if shown < SHOW_PKTS:
            code = pkt.partition(b'\x01')[2].partition(b'\x02')[0].partition(b'\x04')[0]
            text = pkt.partition(b'\x02')[2].partition(b'\x04')[0]
            try:
                pos = int(code[-4:]) + 1          # 1-based, like daksports.json
            except ValueError:
                pos = '?'
            print('  packet pos {:>4}  text {!r}'.format(pos, text))
            shown += 1
    if len(buf) > 2000:     # no ETX in a long stretch — keep memory bounded
        buf = buf[-500:]

print()
print('Bytes received:  ', total)
print('Packets decoded: ', pkts)
if first:
    print('First bytes (hex):', ' '.join('{:02x}'.format(b) for b in first))
print()
if total == 0:
    print('VERDICT: nothing on the serial input. Check the RTD cable (DB25 pin 2 / pin 7 GND),')
    print('the MAX3232 power (3V3/GND) and R1OUT -> GPIO{}, and that the console is'.format(rx))
    print('running a sport code with RTD output.')
elif pkts == 0:
    print('VERDICT: bytes arrive but no RTD packets. Usually wrong baud rate, a missing')
    print('or miswired level shifter (inverted/garbled data), or loose ground.')
else:
    print('VERDICT: RTD data is arriving ({} packets in {} s). The serial side works.'.format(
        pkts, SECONDS))
