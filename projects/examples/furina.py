#!/usr/bin/env python3
"""
Furina (Focalors) — Hydro Archon of Fontaine
Full-body ANSI ASCII portrait for Windows Terminal / cmd.exe
"""
import os
import sys


# ── Terminal Setup ─────────────────────────────────────────────────────────────

def enable_windows_ansi():
    """Enable ANSI VT processing and UTF-8 on Windows."""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


def clear_screen():
    os.system('cls' if sys.platform == 'win32' else 'clear')


def wait_for_keypress():
    sys.stdout.flush()
    if sys.platform == 'win32':
        try:
            import msvcrt
            msvcrt.getch()
            return
        except Exception:
            pass
    try:
        import tty, termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        input()


# ── ANSI Color Palette ─────────────────────────────────────────────────────────
RS = '\033[0m'           # Reset
_b = '\033[1m'           # Bold
NB = '\033[34m'          # Navy Blue  — hat crown, coat
BB = '\033[94m'          # Bright Blue — coat lapel highlights
CY = '\033[96m'          # Cyan — hair
WH = '\033[97m'          # White — vest, jabot, ankle ruffles
GD = '\033[33m'          # Gold — sash trim, buttons, thigh band
LG = '\033[93m'          # Light Gold — vision gem, sparkle accents
DG = '\033[90m'          # Dark Gray — legs/shadow
BK = '\033[30m'          # Black — shoes
SK = '\033[38;5;224m'    # Peach — skin tone
PM = '\033[35m'          # Plum/Pink — lips, blush
AQ = '\033[36m'          # Aqua/Teal — left eye (Ousia)

# Combined shortcuts
n  = NB + _b             # navy bold  (hat/coat main)
b  = BB + _b             # bright blue bold
c  = CY + _b             # cyan bold (hair)
w  = WH + _b             # white bold (jabot/vest)
g  = GD + _b             # gold bold
lg = LG + _b             # light gold bold
bk = BK + _b             # black bold (shoes)
sk = SK                  # skin
pm = PM + _b             # pink/plum bold
aq = AQ + _b             # aqua/teal bold
dg = DG                  # dark gray
R  = RS                  # short reset


# ── Portrait ───────────────────────────────────────────────────────────────────
# Each string's visible width is ~56 chars (ANSI codes are zero-width in terminal)

PORTRAIT = [
    "",
    # ─── TOP HAT ────────────────────────────────────────────────────
    #     The hat is tilted slightly right — very Furina
    f"                   {n}___________  {R}",
    f"                  {n}|           | {R}",
    f"                  {n}|{lg}*{g}~{lg}*{g}~{lg}*{g}~{lg}*{g}~{lg}*{n}| {R}",
    f"                  {n}|{g}~~~~~~~~~~~{n}| {R}",
    f"                  {n}|{lg}*{g}~{lg}*{g}~{lg}*{g}~{lg}*{g}~{lg}*{n}| {R}",
    f"                  {n}|___________|  {R}",
    f"              {n}____|___________|____  {R}",
    f"             {n}/_____________________|{R}",
    # ─── HAIR (framing the hat brim) ────────────────────────────────
    f"          {c}/\\{n}/___{sk}               {n}___\\{c}/\\{R}",
    f"         {c}//   {n}/{sk}  _             _  {n}\\   {c}\\\\{R}",
    f"        {c}//   {n}/ {sk} / \\           / \\  {n}\\   {c}\\\\{R}",
    # ─── FACE ───────────────────────────────────────────────────────
    f"       {c}||  {n}| {sk}  |{aq}o o{sk}|   ___   |{lg}o o{sk}|  {n}|  {c}||{R}",
    f"       {c}||  {n}| {sk}  | {aq}---{sk}|  /   \\  |{lg}---{sk}|  {n}|  {c}||{R}",
    f"       {c}||  {n}| {sk}   \\ {aq}_{sk} / {sk} | ~ |   \\ {lg}_{sk}/   {n}|  {c}||{R}",
    f"       {c}||  {n}| {sk}       ~  {pm}( u ){sk}   ~      {n}|  {c}||{R}",
    f"        {c}\\\\  {n}\\{sk}  _______________________  {n}/  {c}//{R}",
    f"         {c}\\\\  {n}\\_{sk}________________________{n}_/  {c}//{R}",
    # ─── NECK ───────────────────────────────────────────────────────
    f"          {c}\\\\   {sk}   |||||||||||   {c}   //{R}",
    f"           {c}\\\\  {sk}   |||||||||     {c}  //{R}",
    # ─── JABOT (white ruffled collar) ───────────────────────────────
    f"            {w}  /\\ ||||||||| /\\  {R}",
    f"            {w} /  \\~~~~~~~~~/ \\  {R}",
    f"            {w}/    \\~~~~~~~/ \\   \\{R}",
    f"            {w}|   ~~\\~~~~~/ ~~   |{R}",
    f"            {w}|  ~~~~\\~~~/ ~~~~  |{R}",
    f"            {w}|  ~~~~~~~~~~~~~~~~|{R}",
    f"            {w}|  ~~~~~~~~~~~~~~~~|{R}",
    f"            {w} \\  ~~~~~~~~~~~~~~ /{R}",
    # ─── COAT / SHOULDERS ───────────────────────────────────────────
    f"           {n}/  //{WH}                 {n}\\\\  \\ {R}",
    f"          {n}/  //{WH}  _________________  {n}\\\\  \\ {R}",
    f"         {n}/  //{WH}  |                 | {n}\\\\  \\ {R}",
    # ─── VEST & HYDRO VISION ────────────────────────────────────────
    f"         {n}| //{WH}   |  {b}Hydro Vision{WH}   | {n}\\\\  |{R}",
    f"         {n}|//{WH}    |                 | {n}\\\\ |{R}",
    f"         {n}|{WH}      |   {lg}(  *  ){WH}        | {n}   |{R}",
    f"         {n}|{WH}      |   {LG}~~~~~~~~~{WH}       | {n}   |{R}",
    f"         {n}|{WH}      |_________________| {n}   |{R}",
    # ─── SASH & BOW ─────────────────────────────────────────────────
    f"         {n}|  {g}/\\   /===============\\   /\\  {n}|{R}",
    f"         {n}|  {g}/ \\ / {lg}( * gem * ){g} \\ / \\ {n}|{R}",
    f"         {n}|  {g}/   X    ~~~~~~~~~    X   \\ {n}|{R}",
    f"         {n} \\ {g}\\  / \\_________________/ \\  / {n}/  {R}",
    # ─── WAIST / SHORTS ─────────────────────────────────────────────
    f"          {n}  \\{WH}  ___________________  {n}/  {R}",
    f"           {n} |{WH} |                   | {n}|  {R}",
    f"           {n} |{WH} |                   | {n}|  {R}",
    f"           {n} |{WH} |___________________| {n}|  {R}",
    # ─── THIGH BANDS (gold) ─────────────────────────────────────────
    f"           {n} | {g}===================== {n}|  {R}",
    # ─── THIGHS ─────────────────────────────────────────────────────
    f"            {n}\\  {dg}|                 |  {n}/  {R}",
    f"            {n}|  {dg}|                 |  {n}|  {R}",
    f"            {n}|  {dg}|                 |  {n}|  {R}",
    f"            {n}|  {dg}|                 |  {n}|  {R}",
    # ─── ANKLE RUFFLES (white) ──────────────────────────────────────
    f"           {w}/~~\\{dg}|                 |{w}/~~\\ {R}",
    f"          {w}|~~~~|{dg}|                 |{w}|~~~~|{R}",
    f"          {w}|~~~~|{dg}|                 |{w}|~~~~|{R}",
    # ─── HEELED SHOES (black) ───────────────────────────────────────
    f"          {bk}|====|{R}                 {bk}|====|{R}",
    f"          {bk}|    |{R}                 {bk}|    |{R}",
    f"          {bk}|    \\{R}                 {bk}/    |{R}",
    f"          {bk}|_____\\{R}               {bk}/_____|{R}",
    f"          {bk}       \\{R}               {bk}/      {R}",
    f"          {bk}        \\______________{R}{bk}/       {R}",
    "",
]

NAME_CARD = (
    f"  {lg}  ╔══════════════════════════════════════════╗{R}\n"
    f"  {lg}  ║{R}  {n}F U R I N A{R}  {GD}·{R}  {WH}Hydro Archon{R}  {GD}·{R}  {n}Focalors{R}  {lg}║{R}\n"
    f"  {lg}  ╚══════════════════════════════════════════╝{R}"
)

QUOTE = (
    f"\n"
    f"  {n}\"Good morning! Or is it afternoon? Evening?{R}\n"
    f"   {n}Who cares — time revolves around {lg}me{n} anyway.\"{R}\n"
)


def main():
    enable_windows_ansi()
    clear_screen()

    for line in PORTRAIT:
        print(line)

    print(NAME_CARD)
    print(QUOTE)

    print(f"  {dg}[ Press any key to exit ]{R}")
    wait_for_keypress()


if __name__ == '__main__':
    main()
