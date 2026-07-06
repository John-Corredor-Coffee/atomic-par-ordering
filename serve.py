#!/usr/bin/env python3
import http.server
import socketserver

ROOT = '/Users/johncorredor/Desktop/Vault/par-ordering'

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)
    def log_message(self, *args):
        pass

with socketserver.TCPServer(('', 7824), Handler) as httpd:
    httpd.serve_forever()
