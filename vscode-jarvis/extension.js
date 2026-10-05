const vscode = require('vscode');
const cp = require('child_process');
const fs = require('fs');
const path = require('path');

const PARTICIPANT_ID = 'jarvis.voice';
const SYSTEM_PROMPT =
  'You are Jarvis, a voice coding assistant inside VS Code. Your replies are read aloud, so be concise ' +
  'and conversational: lead with the answer, avoid long lists and tables, and keep prose short. ' +
  'Put any code in fenced code blocks; it is shown on screen but not read aloud, so briefly say what it does.';

let server;
let output;
let extensionRoot;

const cfg = () => vscode.workspace.getConfiguration('jarvis');
const baseUrl = () => `http://127.0.0.1:${cfg().get('port')}`;

async function post(route, body) {
  const res = await fetch(baseUrl() + route, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`voice server ${route}: HTTP ${res.status}`);
  return res.json();
}

async function healthy() {
  try {
    return (await fetch(baseUrl() + '/health', { signal: AbortSignal.timeout(1000) })).ok;
  } catch {
    return false;
  }
}

function serverDir() {
  return cfg().get('serverDir') || path.dirname(fs.realpathSync(extensionRoot));
}

async function ensureServer(progress) {
  if (await healthy()) return;
  if (!server) {
    const dir = serverDir();
    const python = path.join(dir, '.venv', 'Scripts', 'python.exe');
    output.appendLine(`Starting ${python} server.py in ${dir}`);
    server = cp.spawn(python, ['-u', 'server.py'], {
      cwd: dir,
      env: { ...process.env, JARVIS_PORT: String(cfg().get('port')) },
      windowsHide: true,
    });
    server.stdout.on('data', (d) => output.append(d.toString()));
    server.stderr.on('data', (d) => output.append(d.toString()));
    server.on('exit', (code) => {
      output.appendLine(`voice server exited (${code})`);
      server = undefined;
    });
  }
  progress?.('Loading voice models (first start takes a few seconds)…');
  for (let i = 0; i < 240; i++) {
    if (await healthy()) return;
    if (!server) break;
    await new Promise((r) => setTimeout(r, 500));
  }
  output.show(true);
  throw new Error('Jarvis voice server did not start. See the "Jarvis Voice" output panel.');
}

/** Turns streamed markdown into spoken sentences, skipping fenced code blocks. */
class Speaker {
  constructor() {
    this.buf = '';
    this.inFence = false;
    this.chain = Promise.resolve();
  }
  say(text) {
    if (!text.trim()) return;
    this.chain = this.chain.then(() => post('/speak', { text })).catch((e) => output.appendLine(String(e)));
  }
  push(chunk) {
    this.buf += chunk;
    for (;;) {
      const fence = this.buf.indexOf('```');
      if (this.inFence) {
        if (fence < 0) {
          this.buf = this.buf.slice(-2); // keep a possible partial fence
          return;
        }
        const eol = this.buf.indexOf('\n', fence);
        this.buf = eol < 0 ? '' : this.buf.slice(eol + 1);
        this.inFence = false;
        continue;
      }
      if (fence >= 0) {
        this.say(this.buf.slice(0, fence));
        this.say("I've put the code in the chat.");
        this.buf = this.buf.slice(fence + 3);
        this.inFence = true;
        continue;
      }
      // Speak up to the last sentence boundary; hold the rest (and trailing backticks) back.
      const m = [...this.buf.matchAll(/[.!?](?=\s)|\n/g)].pop();
      if (m) {
        const end = m.index + m[0].length;
        this.say(this.buf.slice(0, end));
        this.buf = this.buf.slice(end);
      }
      return;
    }
  }
  flush() {
    if (!this.inFence) this.say(this.buf);
    this.buf = '';
    return this.chain;
  }
}

function historyMessages(context) {
  const msgs = [];
  for (const turn of context.history) {
    if (turn.participant !== PARTICIPANT_ID) continue;
    if (turn instanceof vscode.ChatRequestTurn) {
      msgs.push(vscode.LanguageModelChatMessage.User(turn.prompt));
    } else if (turn instanceof vscode.ChatResponseTurn) {
      const text = turn.response
        .filter((p) => p instanceof vscode.ChatResponseMarkdownPart)
        .map((p) => p.value.value)
        .join('')
        // our own "You said" header isn't part of the assistant's answer
        .replace(/^> 🎙️ \*\*You:\*\* (.*)\n\n/, '');
      if (text) msgs.push(vscode.LanguageModelChatMessage.Assistant(text));
    }
  }
  return msgs;
}

async function referenceText(request) {
  const parts = [];
  for (const ref of request.references) {
    try {
      let uri, range;
      if (ref.value instanceof vscode.Uri) uri = ref.value;
      else if (ref.value instanceof vscode.Location) ({ uri, range } = ref.value);
      else if (typeof ref.value === 'string') {
        parts.push(ref.value);
        continue;
      } else continue;
      const doc = await vscode.workspace.openTextDocument(uri);
      const text = doc.getText(range).slice(0, 20000);
      parts.push(`File ${vscode.workspace.asRelativePath(uri)}:\n\`\`\`\n${text}\n\`\`\``);
    } catch {
      /* not a text file */
    }
  }
  return parts.length ? `\n\nContext:\n${parts.join('\n\n')}` : '';
}

async function handler(request, context, stream, token) {
  await ensureServer((msg) => stream.progress(msg));

  let prompt = request.prompt.trim();
  const voiceTurn = request.command === 'talk' || !prompt;
  if (voiceTurn) {
    stream.progress('🎙️ Listening… speak now (pause to finish)');
    const { text } = await post('/listen');
    if (!text) {
      stream.markdown("_I didn't hear anything._");
      return { metadata: { heard: false } };
    }
    stream.markdown(`> 🎙️ **You:** ${text}\n\n`);
    prompt = prompt ? `${prompt}\n\n${text}` : text;
  }

  const speak = voiceTurn || cfg().get('speakTypedPrompts');
  const speaker = new Speaker();
  token.onCancellationRequested(() => post('/stop').catch(() => {}));
  if (speak) await post('/stop'); // interrupt anything still playing from the last answer

  const messages = [
    vscode.LanguageModelChatMessage.User(SYSTEM_PROMPT),
    ...historyMessages(context),
    vscode.LanguageModelChatMessage.User(prompt + (await referenceText(request))),
  ];
  const response = await request.model.sendRequest(messages, {}, token);
  for await (const chunk of response.text) {
    stream.markdown(chunk);
    if (speak) speaker.push(chunk);
  }
  if (speak) await speaker.flush();

  if (voiceTurn && cfg().get('handsFree') && !token.isCancellationRequested) {
    // Re-open the mic once Jarvis stops talking. Runs after this handler returns.
    post('/wait')
      .then(() => vscode.commands.executeCommand('jarvis.talk'))
      .catch((e) => output.appendLine(String(e)));
  }
  return { metadata: { heard: voiceTurn } };
}

function activate(context) {
  extensionRoot = context.extensionPath;
  output = vscode.window.createOutputChannel('Jarvis Voice');

  const participant = vscode.chat.createChatParticipant(PARTICIPANT_ID, handler);
  participant.iconPath = new vscode.ThemeIcon('mic');

  context.subscriptions.push(
    output,
    participant,
    vscode.commands.registerCommand('jarvis.talk', () =>
      vscode.commands.executeCommand('workbench.action.chat.open', { query: '@jarvis /talk', isPartialQuery: false })
    ),
    vscode.commands.registerCommand('jarvis.stop', () => post('/stop').catch(() => {})),
    vscode.commands.registerCommand('jarvis.restartServer', async () => {
      server?.kill();
      server = undefined;
      await new Promise((r) => setTimeout(r, 1000));
      await ensureServer();
      vscode.window.showInformationMessage('Jarvis voice server is ready.');
    })
  );

  // Warm the models in the background so the first question is fast.
  ensureServer().catch((e) => output.appendLine(String(e)));
}

function deactivate() {
  server?.kill();
}

module.exports = { activate, deactivate };
