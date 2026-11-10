"""Entry point for ``python -I -m o2j``.

The ``-I`` form is the documented way to run this tool: isolated mode keeps the current
directory off ``sys.path``, so a spec directory cannot shadow a stdlib module. A planted
``yaml.py`` next to an ingested spec must never load.
"""

from o2j.cli import app

if __name__ == "__main__":
    app()
