"""Print a private console login URL only when explicitly invoked by the operator."""
import argparse
from pathlib import Path
from urllib.parse import quote

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--data-dir',default='./data')
parser.add_argument('--origin',default='http://127.0.0.1:18080')
args=parser.parse_args()
token=(Path(args.data_dir)/'console_token').read_text().strip()
print(args.origin.rstrip('/')+'/console-login?token='+quote(token))
