# Runtime requirements

The renderer requires Python 3.11 or newer and Pillow 9.4 through 11.x.

Check the runtime before using the bundled script:

```bash
python3 -c "import PIL; print(PIL.__version__)"
```

If Pillow is unavailable, stop and tell the user that preview and delivery cannot run yet. Installing a dependency changes the user's Python environment, so obtain authorization before running:

```bash
python3 -m pip install -r requirements.txt
```

Run that command from this skill directory. Do not install packages during an unattended refresh.
