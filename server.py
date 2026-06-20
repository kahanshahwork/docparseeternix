"""
server.py — kept only so any existing shortcut/script that runs
`python server.py` still works. All real wiring now lives in app.py;
all endpoint logic lives in routes/parser_routes.py + routes/workflow_routes.py.
"""

from app import create_app

app = create_app()

if __name__ == "__main__":
    print("\n🟢  DocParse  →  http://localhost:5050\n")
    app.run(port=5050, debug=False)
