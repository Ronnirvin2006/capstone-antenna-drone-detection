# HackRF 2.4 GHz GNU Radio Monitor

This is the main monitor to use instead of the browser waterfall.
It uses GNU Radio + osmosdr + HackRF directly, with a spectrum trace and waterfall like Gqrx.

## Run

Close Gqrx first. Only one program can use the HackRF.

```bash
cd /home/ron/capstone
python3 gnuradio/hackrf_24ghz_monitor.py
```

## Starting Settings

- Antenna path: `Vivaldi antenna -> coax -> HackRF RX/ANT`
- Center frequency: `2400.000000 MHz`
- Visible span: `10 MHz`, so the display shows about `2395 MHz` to `2405 MHz`
- LNA gain: `16 dB`
- VGA gain: `20 dB`
- RF amp: `off`
- Waterfall floor/ceiling: start around `-98 dB` and `-38 dB`

If the waterfall is too yellow/white, lower the waterfall ceiling or reduce gain.
If everything is dark blue, increase VGA gain slowly or raise the waterfall floor.

## Drone Test

Use repeatable steps:

1. Keep controller and drone off for 10 seconds.
2. Turn controller on and watch for new lines or bursts.
3. Turn drone on and watch again.
4. Move the center frequency to any strong area, for example `2404.045300 MHz`.

A visible line or burst is RF activity, not final drone classification yet.
For proof, it must appear when the drone/controller is on and disappear when they are off.

## If HackRF Does Not Open

Run:

```bash
hackrf_info
```

If Gqrx is open, close it. If another app is using HackRF, close that app and reconnect the HackRF.
