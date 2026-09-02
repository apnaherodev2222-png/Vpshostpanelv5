"""
script_scanner.py
──────────────────────────────────────────────────────────────────────────
Fast, static, text-pattern scanner used by approval_bot.py to auto-clear
obviously-benign free-tier uploads and flag scripts that look like they
implement network abuse (DDoS/flood tooling, mass port scanning,
credential brute-forcing, crypto-mining, botnet/C2 patterns, destructive
filesystem commands, etc).

Important limits, by design:
  - This NEVER executes the uploaded file. It only reads the text.
  - It's a fast first-pass filter, not a verdict of intent — a human
    admin can always override its decision from the admin panel.
  - It only stores/matches short category labels, never full technique
    detail, so a 'flagged' notice tells the user WHAT category tripped
    without turning this into a how-to reference.
"""
import re
from pathlib import Path

# (pattern, short category label, severity)
SUSPICIOUS_PATTERNS = [
    (r'\bhping3\b', 'packet-flood tooling', 'high'),
    (r'\bmasscan\b', 'mass network scanning tooling', 'high'),
    (r'\bnmap\b[^\n]{0,40}-p\s*0-65535', 'mass port scanning', 'high'),
    (r'\bslowloris\b', 'DoS pattern', 'high'),
    (r'\bLOIC\b|\bHOIC\b', 'known DDoS tool reference', 'high'),
    (r'SYN[_ -]?flood|UDP[_ -]?flood|ICMP[_ -]?flood', 'flood-attack pattern', 'high'),
    (r'\bscapy\b[\s\S]{0,60}(send|sr1|flood)', 'raw packet crafting/flood', 'high'),
    (r'\bsocket\.SOCK_RAW\b', 'raw socket usage', 'medium'),
    (r'\bhydra\b|\bmedusa\b|\bncrack\b', 'credential brute-force tooling', 'high'),
    (r'\bsqlmap\b', 'SQL-injection exploitation tooling', 'high'),
    (r'while\s+True\s*:[\s\S]{0,80}requests\.(get|post)', 'unbounded HTTP flood loop', 'medium'),
    (r'\bxmrig\b|stratum\+tcp://|\bcryptonight\b', 'crypto-mining indicators', 'high'),
    (r'/etc/shadow|/etc/passwd', 'sensitive host file access', 'medium'),
    (r'\bparamiko\b', 'SSH client library usage', 'medium'),
    (r'\btelnetlib\b|\btelnetlib3\b', 'raw telnet client usage', 'medium'),
    (r'\bbotnet\b|\bC2[_ -]?server\b|command[_ -]?and[_ -]?control', 'botnet/C2 terminology', 'medium'),
    (r'\bmirai\b|\bgafgyt\b|\bqbot\b', 'known IoT-botnet family reference', 'high'),
    (r'rm\s+-rf\s+/(?!\S)', 'destructive filesystem wipe', 'high'),
    (r'os\.system\([^\n]*mkfs', 'disk formatting command', 'high'),
    (r'\bshodan\b|\bcensys\b|\bzoomeye\b', 'internet-wide host search API usage', 'medium'),
    # mass credential-list usage — reading a big list of user/pass pairs to try in bulk
    (r'(wordlist|combolist|userlist|passlist|creds?_list)\s*=', 'bulk credential-list usage', 'medium'),
    (r"(admin|root)['\"]?\s*[,:]\s*['\"](admin|root|password|toor|12345)", 'default-credential list', 'medium'),
    # mass IP/host iteration combined with a connection attempt in the same loop —
    # the shape of a range scanner regardless of which library it uses
    (r'ip_network\([\s\S]{0,200}(socket\.connect|\.connect_ex|paramiko|telnetlib)', 'IP-range iteration + connection attempt', 'high'),
    (r'for\s+\w+\s+in\s+range\([\s\S]{0,120}\)\s*:[\s\S]{0,200}socket\.connect', 'ranged loop + raw socket connect', 'medium'),
    (r'ThreadPoolExecutor[\s\S]{0,150}(socket\.connect|paramiko|telnetlib)', 'multi-threaded mass connection attempts', 'medium'),
]

MAX_SCAN_BYTES = 2_000_000  # don't choke on huge uploads


def scan_file(path: Path):
    """
    Returns (verdict, findings):
      verdict   'clear' | 'flagged'
      findings  list of (category_label, severity) tuples that matched

    Verdict logic: any single 'high' severity hit, OR two-plus hits total,
    flags the script for rejection + mute. A lone 'medium' hit is noted
    but doesn't block on its own (keeps false-positive rate reasonable —
    e.g. a legitimate monitoring script touching socket.SOCK_RAW once).
    """
    path = Path(path)
    try:
        raw = path.read_bytes()[:MAX_SCAN_BYTES]
        text = raw.decode('utf-8', errors='ignore')
    except Exception:
        return 'clear', []

    findings = []
    for pattern, label, severity in SUSPICIOUS_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            findings.append((label, severity))

    has_high = any(sev == 'high' for _, sev in findings)
    verdict = 'flagged' if has_high or len(findings) >= 2 else 'clear'
    return verdict, findings
