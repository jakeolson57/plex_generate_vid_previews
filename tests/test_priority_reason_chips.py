from pathlib import Path
import json
import shutil
import subprocess
import pytest


def evaluate(expression):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to exercise dashboard presentation helpers')
    common = Path('plex_generate_previews/web/static/common.js').read_text()
    script = Path('plex_generate_previews/web/static/dashboard.js').read_text().rsplit('init();', 1)[0]
    dom = "const document={documentElement:{dataset:{}},getElementById:()=>({classList:{toggle(){}}})};const localStorage={getItem(){return null},setItem(){}};"
    result = subprocess.run([node, '-e', dom+common+script+'console.log(JSON.stringify('+expression+'));'], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_index_template_renders_priority_reason_chips():
    labels = evaluate("visibleReasons({priority_reasons:[{type:'next_episode'},{type:'on_deck'},{type:'active_next'},{type:'next_episode'}]})")
    assert labels == ['Watch history', 'On Deck', 'Watching next']


def test_hub_priority_chip_uses_specific_hub_name():
    assert evaluate("visibleReasons({priority_reasons:[{type:'hub',hub:'Favourite TV'}]})") == ['Favourite TV']
    assert '&lt;script&gt;' in evaluate("chip('<script>')")
