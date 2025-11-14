"""Small helper to generate a password hash for CCTV app.
Usage:
    python create_password.py
It will prompt for a password and print a hash which you should store in the CCTV_PASSWORD_HASH secret/env var.
"""
from getpass import getpass
from werkzeug.security import generate_password_hash

if __name__ == '__main__':
    p = getpass('Password: ')
    h = generate_password_hash(p)
    print('\nAdd this value as CCTV_PASSWORD_HASH (keep it secret):\n')
    print(h)
