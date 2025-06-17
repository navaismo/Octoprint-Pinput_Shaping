#!/usr/bin/env python3
"""
adxl345usb – Fly-ADXL345-USB wrapper (Pinput-Shaping-compatible)

Exit codes
==========    0  OK
              1  bad command-line
              2  could not open serial port
              3  cannot open save-file
              4  runtime/parsing error
"""
import argparse, os, select, sys, termios, tty, time
from textwrap import dedent

try:
    import serial
    from serial import SerialException
except ModuleNotFoundError:
    sys.stderr.write("ERROR: pyserial not installed.  Try:  sudo pip3 install pyserial\n")
    sys.exit(2)

# ─────────── helper ─────────────────────────────────────────────────
def die(msg: str, code: int):
    sys.stderr.write(f"ERROR: {msg}\n")
    sys.exit(code)

# ─────────── args ──────────────────────────────────────────────────
ap = argparse.ArgumentParser(
    formatter_class=argparse.RawDescriptionHelpFormatter,
    description="Stream ADXL345 data from an RP2040 board.",
    epilog=dedent("""\
        Behaviour (matches navaismo/adxl345spi):
          * without -s  → human-readable rows on STDOUT
          * with    -s  → CSV written to FILE, STDOUT gets only the banner
          * stop      by:  • timeout (-t)  • or sending 'q'+'Enter' to stdin
    """))

ap.add_argument("-p", "--port", default="/dev/ttyACM0", help="serial device")
ap.add_argument("-f", "--freq", type=int, default=250, help="sampling rate 1-3200 Hz")
ap.add_argument("-t", "--time", type=int, default=0,   help="capture seconds (0 = until Q)")
ap.add_argument("-s", "--save", metavar="FILE", help="CSV output file")
ap.add_argument("--sensor", type=int, choices=[0,1], default=0,
                help="when firmware outputs two sensors choose which one to use")
ap.add_argument("--dual", action="store_true",
                help="if firmware outputs two sensors save/show both instead of one")
args = ap.parse_args()

if not (1 <= args.freq <= 3200):
    die("frequency must be 1–3200 Hz", 1)

# ─────────── open serial ────────────────────────────────────────────
try:
    baud = int(os.getenv("ADXLUSB_BAUD", "2000000"))
    ser  = serial.Serial(args.port, baud, timeout=0.1)
except SerialException as e:
    die(f"cannot open {args.port} – {e}", 2)

ser.reset_input_buffer()
ser.write(f"F={args.freq}\n".encode())         # firmware resends header

# ─────────── banner for plugin ──────────────────────────────────────
print("Press Q to stop", flush=True)

# ─────────── pick output sink ───────────────────────────────────────
if args.save:
    try:
        csv = open(args.save, "w", buffering=1)
    except OSError as e:
        die(f"cannot open '{args.save}' for writing – {e}", 3)
    FLUSH_EVERY = 1000
else:
    csv = None

# ─────────── raw-mode stdin to catch single keys ────────────────────
fd_stdin = sys.stdin.fileno()
old_attr  = termios.tcgetattr(fd_stdin)
tty.setcbreak(fd_stdin)

def flush_buffer():
    """placeholder; will be replaced after header parsing"""
    pass

try:
    # ───── determine output format from header ─────────────────────
    dual_output = False
    header = ""
    while True:
        hdr = ser.readline()
        if hdr.startswith(b"time,"):
            header = hdr.decode().strip()
            if header == "time,x0,y0,z0,x1,y1,z1":
                dual_output = True
            elif header != "time,x,y,z":
                die(f"unexpected header '{header}'", 4)
            break

    if args.dual and not dual_output:
        sys.stderr.write("WARNING: --dual ignored, firmware outputs a single sensor\n")

    save_dual = dual_output and args.dual

    if csv:
        if save_dual:
            csv.write("time,x0,y0,z0,x1,y1,z1\n")
            buf_t, buf_x0, buf_y0, buf_z0 = [], [], [], []
            buf_x1, buf_y1, buf_z1 = [], [], []
            def flush_buffer():
                for t,x0,y0,z0,x1,y1,z1 in zip(buf_t, buf_x0, buf_y0, buf_z0,
                                                buf_x1, buf_y1, buf_z1):
                    csv.write(f"{t:.6f},{x0:.6f},{y0:.6f},{z0:.6f},"
                              f"{x1:.6f},{y1:.6f},{z1:.6f}\n")
                buf_t.clear(); buf_x0.clear(); buf_y0.clear(); buf_z0.clear()
                buf_x1.clear(); buf_y1.clear(); buf_z1.clear()
                csv.flush()
        else:
            csv.write("time,x,y,z\n")
            buf_t, buf_x, buf_y, buf_z = [], [], [], []
            def flush_buffer():
                for t,x,y,z in zip(buf_t, buf_x, buf_y, buf_z):
                    csv.write(f"{t:.6f},{x:.6f},{y:.6f},{z:.6f}\n")
                buf_t.clear(); buf_x.clear(); buf_y.clear(); buf_z.clear()
                csv.flush()

    t_start   = time.time()
    t_end     = t_start + args.time if args.time else None
    samples   = 0
    bad_lines = 0

    while True:
        # ---- termination conditions --------------------------------
        if select.select([fd_stdin], [], [], 0)[0]:
            if sys.stdin.read(1).lower() == "q":
                break
        if t_end and time.time() >= t_end:
            break

        # ---- read one CSV line -------------------------------------
        line = ser.readline()
        if not line:
            continue
        try:
            parts = line.decode().strip().split(',')
            if dual_output:
                t_s, x0_s, y0_s, z0_s, x1_s, y1_s, z1_s = parts
                t_f = float(t_s)
                vals0 = (float(x0_s), float(y0_s), float(z0_s))
                vals1 = (float(x1_s), float(y1_s), float(z1_s))
            else:
                t_s, x_s, y_s, z_s = parts
                t_f = float(t_s)
                vals0 = (float(x_s), float(y_s), float(z_s))
        except ValueError:
            bad_lines += 1
            if bad_lines > 10:
                die("too many malformed lines from firmware", 4)
            continue

        if csv:
            if save_dual:
                buf_t.append(t_f)
                buf_x0.append(vals0[0]); buf_y0.append(vals0[1]); buf_z0.append(vals0[2])
                buf_x1.append(vals1[0]); buf_y1.append(vals1[1]); buf_z1.append(vals1[2])
            else:
                if dual_output:
                    chosen = vals0 if args.sensor == 0 else vals1
                else:
                    chosen = vals0
                x_f, y_f, z_f = chosen
                buf_t.append(t_f); buf_x.append(x_f); buf_y.append(y_f); buf_z.append(z_f)
            if len(buf_t) >= FLUSH_EVERY:
                flush_buffer()
        else:
            if dual_output and args.dual:
                print(
                    f"time = {t_f:.3f}, x0 = {vals0[0]:.3f}, y0 = {vals0[1]:.3f}, z0 = {vals0[2]:.3f}, "
                    f"x1 = {vals1[0]:.3f}, y1 = {vals1[1]:.3f}, z1 = {vals1[2]:.3f}")
            else:
                if dual_output:
                    chosen = vals0 if args.sensor == 0 else vals1
                else:
                    chosen = vals0
                print(f"time = {t_f:.3f}, x = {chosen[0]:.3f}, y = {chosen[1]:.3f}, z = {chosen[2]:.3f}")
        samples += 1

finally:
    # restore TTY + close resources
    termios.tcsetattr(fd_stdin, termios.TCSADRAIN, old_attr)
    ser.close()
    if csv:
        flush_buffer()
        csv.close()

# ─────────── summary line (only when not csv-save) ──────────────────
elapsed = time.time() - t_start
if not csv:
    sys.stderr.write(f"Captured {samples} samples in {elapsed:.2f} s "
                     f"({samples/elapsed:.1f} Hz)\n")
sys.exit(0)
