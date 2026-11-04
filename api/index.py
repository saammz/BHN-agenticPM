"""Vercel entry point. Vercel's Python runtime serves any BaseHTTPRequestHandler subclass named `handler`;
vercel.json rewrites every path here, and the app's own router takes it from there."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from server import Handler as handler  # noqa: E402,F401
