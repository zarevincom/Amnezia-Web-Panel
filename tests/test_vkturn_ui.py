"""Exercise actual dialog functions, including a superseded private-key export."""
import shutil
import subprocess
from pathlib import Path
import unittest


@unittest.skipUnless(shutil.which('node'), 'node is required')
class VkTurnDialogTests(unittest.TestCase):
    def test_old_profile_response_cannot_populate_new_profile_dialog(self):
        source = (Path(__file__).resolve().parents[1] / 'templates/server.html').read_text(encoding='utf-8')
        script = source.split('    let vkTurnClientId = null;', 1)[1].split('    async function showConnectionConfig', 1)[0]
        harness = r'''
const assert = require('node:assert/strict');
const elements = new Map();
const document = {getElementById(id) {
  if (!elements.has(id)) elements.set(id, {value:'', disabled:false,
    classList: {hidden:true, add(){this.hidden=true}, remove(){this.hidden=false}, toggle(k,v){this.hidden=v}}});
  return elements.get(id);
}};
let finishOldExport;
const apiCall = (url, method) => method === 'POST'
  ? new Promise(resolve => {finishOldExport=resolve})
  : Promise.resolve({endpoint:'host:56000',wrap_key:'key',version:'v1.10.1',backend_port:51820});
const SERVER_ID=0;
const _ = key => key;
const openModal = () => {};
const showToast = message => {throw Error(message)};
let vkTurnClientId = null;
'''
        harness += script + r'''
(async () => {
  await openVkTurnSettings('alice', 'Alice');
  const oldExport = exportVkTurnClient();
  await openVkTurnSettings('bob', 'Bob');
  finishOldExport({config:'ALICE PRIVATE KEY',ios_link:'secret',client_command:'secret'});
  await oldExport;
  assert.equal(document.getElementById('vkTurnExportResult').classList.hidden, true);
  assert.equal(document.getElementById('vkTurnConfig').value, '');
})().catch(error => {console.error(error);process.exitCode=1});
'''
        result = subprocess.run(['node', '-e', harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
