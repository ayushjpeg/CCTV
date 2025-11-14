#!/usr/bin/env python
"""Minimal test to see if Flask + Socket.IO works"""

from flask import Flask

app = Flask(__name__)

@app.route('/test')
def test():
    return 'Hello World'

if __name__ == '__main__':
    print('Starting minimal test server on 8002...')
    app.run(host='0.0.0.0', port=8002, debug=False)
