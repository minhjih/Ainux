"""Minimal web UI for Ainux natural-language orchestration."""

from __future__ import annotations

import json
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from ..client import ChatClient, ChatClientError
from ..config import ConfigError, resolve_provider
from ..context import default_fabric_path, load_fabric
from ..orchestration import AinuxOrchestrator, OrchestrationError
from .assets import AINUX_LOGO_DATA_URI, AINUX_PENGUIN_DATA_URI


INDEX_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Ainux Orchestration Studio</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #050b1f;
      --panel: rgba(10, 18, 34, 0.72);
      --panel-border: rgba(148, 163, 184, 0.18);
      --card: rgba(17, 27, 48, 0.88);
      --accent: #38bdf8;
      --accent-soft: rgba(56, 189, 248, 0.18);
      --accent-strong: rgba(56, 189, 248, 0.35);
      --text-primary: #e7f1ff;
      --text-muted: #8fa3c4;
      --warn: #facc15;
      --danger: #f87171;
      --success: #34d399;
      font-family: "Pretendard", "Inter", "Noto Sans KR", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: radial-gradient(circle at 15% 15%, rgba(73, 142, 255, 0.18), transparent 55%),
                  radial-gradient(circle at 80% 10%, rgba(45, 212, 191, 0.12), transparent 50%),
                  linear-gradient(160deg, #030712 0%, #0b1430 55%, #111c3d 100%);
      color: var(--text-primary);
      display: flex;
      align-items: stretch;
      justify-content: center;
      padding: clamp(16px, 3vw, 28px);
      position: relative;
      overflow-x: hidden;
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background:
        radial-gradient(circle at 60% 20%, rgba(56, 189, 248, 0.18), transparent 60%),
        var(--ainux-logo) center 6vh / min(68vw, 640px) no-repeat;
      opacity: 0.22;
      filter: saturate(120%) blur(0px);
      z-index: 0;
    }
    .app {
      position: relative;
      z-index: 1;
      max-width: 1300px;
      width: 100%;
      display: flex;
      flex-direction: column;
      gap: 22px;
    }
    .studio-hero {
      position: relative;
      overflow: hidden;
      border-radius: 22px;
      padding: clamp(22px, 5vw, 34px);
      display: grid;
      grid-template-columns: minmax(240px, 320px) 1fr;
      gap: clamp(18px, 4vw, 34px);
      align-items: center;
      backdrop-filter: blur(14px);
      background: linear-gradient(140deg, rgba(15, 33, 68, 0.88) 0%, rgba(28, 48, 92, 0.78) 45%, rgba(21, 88, 146, 0.72) 100%);
      border: 1px solid rgba(148, 163, 184, 0.22);
      box-shadow: 0 30px 80px rgba(7, 15, 35, 0.55);
    }
    .studio-hero::after {
      content: "";
      position: absolute;
      inset: 0;
      background: radial-gradient(circle at 20% 0%, rgba(56, 189, 248, 0.25), transparent 55%),
                  radial-gradient(circle at 90% 20%, rgba(14, 165, 233, 0.18), transparent 60%);
      opacity: 0.65;
      pointer-events: none;
    }
    .hero-visual {
      position: relative;
      width: 100%;
      aspect-ratio: 1;
      border-radius: 26px;
      background: rgba(8, 15, 32, 0.45);
      border: 1px solid rgba(148, 163, 184, 0.18);
      box-shadow: inset 0 0 0 1px rgba(148, 163, 184, 0.12), 0 20px 45px rgba(6, 18, 40, 0.6);
      display: grid;
      place-items: center;
      overflow: hidden;
      isolation: isolate;
    }
    .hero-logo {
      position: absolute;
      inset: 0;
      background: var(--ainux-logo) center / cover no-repeat;
      opacity: 0.92;
      filter: drop-shadow(0 20px 45px rgba(3, 8, 21, 0.55));
      transform: scale(1.02);
    }
    .hero-mascot {
      position: relative;
      width: 68%;
      max-width: 240px;
      border-radius: 28px;
      box-shadow: 0 22px 60px rgba(8, 22, 46, 0.65);
      border: 1px solid rgba(255, 255, 255, 0.12);
      backdrop-filter: blur(6px);
      padding: 12px;
      background: linear-gradient(160deg, rgba(15, 31, 64, 0.75), rgba(23, 47, 96, 0.75));
    }
    .hero-copy {
      position: relative;
      z-index: 1;
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .hero-title {
      margin: 0;
      font-size: clamp(1.6rem, 3vw, 2.1rem);
      letter-spacing: -0.03em;
    }
    .hero-subtitle {
      margin: 0;
      font-size: clamp(1rem, 1.6vw, 1.15rem);
      color: rgba(224, 236, 255, 0.82);
      line-height: 1.6;
    }
    .hero-badges {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 6px;
    }
    .badge {
      padding: 5px 12px;
      border-radius: 999px;
      font-size: 0.76rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      background: var(--accent-soft);
      color: var(--accent);
      font-weight: 600;
    }
    .badge.offline { background: rgba(148, 163, 184, 0.18); color: var(--text-muted); }
    .badge.execute { background: rgba(52, 211, 153, 0.18); color: var(--success); }
    .badge.dryrun { background: rgba(248, 113, 113, 0.18); color: var(--danger); }
    .workspace {
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
      gap: 20px;
      min-height: 540px;
    }
    .panel {
      backdrop-filter: blur(12px);
      background: var(--panel);
      border-radius: 20px;
      border: 1px solid var(--panel-border);
      display: flex;
      flex-direction: column;
      overflow: hidden;
      box-shadow: 0 16px 42px rgba(8, 16, 34, 0.55);
    }
    .panel header {
      border-radius: 0;
      border: none;
      box-shadow: inset 0 -1px 0 rgba(148, 163, 184, 0.12);
      padding: 20px 26px;
      background: rgba(8, 15, 32, 0.62);
    }
    .panel header h2 {
      margin: 0;
      font-size: 1.05rem;
      letter-spacing: -0.01em;
    }
    .scroll-area {
      flex: 1;
      overflow-y: auto;
      padding: 22px 26px;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }
    .interaction {
      background: var(--card);
      border-radius: 18px;
      border: 1px solid rgba(148, 163, 184, 0.16);
      padding: 20px 22px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      position: relative;
    }
    .interaction::before {
      content: "";
      position: absolute;
      inset: 0;
      border-radius: 18px;
      padding: 1px;
      background: linear-gradient(160deg, rgba(56, 189, 248, 0.35), transparent 45%);
      -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
      -webkit-mask-composite: xor;
      mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
      mask-composite: exclude;
      opacity: 0;
      transition: opacity 0.3s ease;
      pointer-events: none;
    }
    .interaction:hover::before { opacity: 1; }
    .interaction-header {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      font-size: 0.86rem;
      color: var(--text-muted);
      align-items: center;
      justify-content: space-between;
    }
    .request-text {
      font-size: 1rem;
      line-height: 1.55;
      white-space: pre-wrap;
    }
    .summary {
      background: rgba(56, 189, 248, 0.1);
      border-radius: 14px;
      padding: 16px 18px;
      border: 1px solid rgba(56, 189, 248, 0.22);
      line-height: 1.5;
      font-size: 0.95rem;
    }
    .warnings { color: var(--warn); font-size: 0.85rem; }
    .error {
      color: var(--danger);
      font-size: 0.9rem;
      background: rgba(248, 113, 113, 0.12);
      border-radius: 12px;
      padding: 12px 14px;
    }
    .command-card {
      border-radius: 16px;
      padding: 18px 20px;
      background: rgba(12, 22, 46, 0.82);
      border: 1px solid rgba(148, 163, 184, 0.15);
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .command-card h3 { margin: 0; font-size: 0.95rem; }
    .command-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      font-size: 0.8rem;
      color: var(--text-muted);
    }
    .execution-output {
      background: rgba(56, 189, 248, 0.07);
      border-left: 3px solid rgba(56, 189, 248, 0.38);
      padding: 12px 14px;
      font-family: "JetBrains Mono", "Fira Code", "SFMono-Regular", monospace;
      font-size: 0.82rem;
      white-space: pre-wrap;
      color: #d0defb;
    }
    .fabric-card {
      border-radius: 16px;
      padding: 18px 20px;
      background: rgba(10, 19, 40, 0.72);
      border: 1px solid rgba(148, 163, 184, 0.15);
      font-size: 0.9rem;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    form {
      display: flex;
      flex-direction: column;
      gap: 14px;
      padding: clamp(20px, 4vw, 26px);
      backdrop-filter: blur(12px);
      background: var(--panel);
      border-radius: 20px;
      border: 1px solid var(--panel-border);
      box-shadow: 0 18px 42px rgba(8, 16, 34, 0.5);
    }
    textarea {
      min-height: 120px;
      resize: vertical;
      border-radius: 16px;
      border: 1px solid rgba(148, 163, 184, 0.25);
      background: rgba(6, 12, 26, 0.85);
      color: var(--text-primary);
      padding: 18px;
      font-size: 1rem;
      line-height: 1.55;
    }
    textarea:focus {
      outline: none;
      border-color: rgba(56, 189, 248, 0.68);
      box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.25);
    }
    .controls {
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      align-items: center;
      justify-content: space-between;
    }
    .toggles {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      font-size: 0.85rem;
      color: var(--text-muted);
    }
    .toggles label {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 7px 12px;
      border-radius: 999px;
      background: rgba(148, 163, 184, 0.16);
      border: 1px solid rgba(148, 163, 184, 0.24);
    }
    .toggles input[type="checkbox"] { accent-color: #38bdf8; }
    .provider {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
    }
    .provider input[type="text"] {
      border-radius: 999px;
      border: 1px solid rgba(148, 163, 184, 0.25);
      background: rgba(6, 12, 26, 0.85);
      color: var(--text-primary);
      padding: 10px 16px;
      min-width: 240px;
    }
    button[type="submit"] {
      border: none;
      border-radius: 999px;
      padding: 12px 26px;
      font-size: 0.98rem;
      font-weight: 600;
      background: linear-gradient(135deg, #38bdf8 0%, #3b82f6 50%, #8b5cf6 100%);
      color: #041021;
      cursor: pointer;
      transition: transform 0.18s ease, box-shadow 0.25s ease;
      box-shadow: 0 14px 36px rgba(56, 189, 248, 0.42);
    }
    button[type="submit"]:hover {
      transform: translateY(-1px) scale(1.01);
      box-shadow: 0 18px 44px rgba(56, 189, 248, 0.55);
    }
    button[type="submit"]:active { transform: translateY(1px); }
    @media (max-width: 1080px) {
      .studio-hero { grid-template-columns: 1fr; text-align: center; }
      .hero-visual { margin: 0 auto; max-width: 320px; }
      .hero-mascot { width: 60%; }
      .workspace { grid-template-columns: 1fr; }
    }
    @media (max-width: 720px) {
      body { padding: 16px; }
      .controls { flex-direction: column; align-items: stretch; }
      .provider { width: 100%; justify-content: space-between; }
      .provider input[type="text"] { flex: 1; min-width: 0; }
      .hero-visual { aspect-ratio: unset; height: auto; }
    }
  </style>
</head>
<body style="--ainux-logo: url('__LOGO_DATA_URI__');">
  <div class="app">
    <header class="studio-hero">
      <div class="hero-visual">
        <div class="hero-logo"></div>
        <img src="__PENGUIN_DATA_URI__" alt="Ainux mascot" class="hero-mascot" />
      </div>
      <div class="hero-copy">
        <h1 class="hero-title">Ainux Orchestration Studio</h1>
        <p class="hero-subtitle">정사각형 로고와 펭귄 마스코트를 품은 Ainux 데스크톱. 자연어로 대화하면, 운영 자동화와 실행 로그가 곧바로 펼쳐집니다.</p>
        <div class="hero-badges">
          <span class="badge">AI 네이티브 OS</span>
          <span class="badge">컨텍스트 패브릭</span>
          <span class="badge">하드웨어 오토파일럿</span>
        </div>
        <div class="hero-badges" style="margin-top: 4px;">
          <span id="status-provider" class="badge">Provider</span>
        </div>
      </div>
    </header>
    <main class="workspace">
      <section class="panel">
        <header><h2>대화 타임라인</h2></header>
        <div class="scroll-area" id="conversation-stream"></div>
      </section>
      <section class="panel">
        <header><h2>플랜 & 명령 로그</h2></header>
        <div class="scroll-area" id="command-log"></div>
      </section>
    </main>
    <section class="panel" style="padding:0;">
      <div class="scroll-area" id="fabric-meta" style="gap:16px;"></div>
    </section>
    <form id="prompt-form">
      <textarea id="prompt-input" placeholder="무엇을 도와드릴까요? 예) GPU 드라이버 상태 점검 보고서를 만들어줘"></textarea>
      <div class="controls">
        <div class="toggles">
          <label><input type="checkbox" id="execute-toggle" /> 실제 명령 실행</label>
          <label><input type="checkbox" id="offline-toggle" /> 오프라인 모드</label>
          <label><input type="checkbox" id="fabric-toggle" checked /> 컨텍스트 패브릭</label>
        </div>
        <div class="provider">
          <input type="text" id="provider-input" placeholder="Provider 이름 (예: openai)" />
          <button type="submit">오케스트레이션 실행</button>
        </div>
      </div>
    </form>
  </div>

  <script>
    (function() {
      const state = {
        history: [],
        fabric: null,
        config: {
          provider: null,
          offline: false,
          execute: false,
          use_fabric: true
        }
      };

      const conversationEl = document.getElementById('conversation-stream');
      const commandEl = document.getElementById('command-log');
      const fabricEl = document.getElementById('fabric-meta');
      const providerBadge = document.getElementById('status-provider');
      const promptInput = document.getElementById('prompt-input');
      const executeToggle = document.getElementById('execute-toggle');
      const offlineToggle = document.getElementById('offline-toggle');
      const fabricToggle = document.getElementById('fabric-toggle');
      const providerInput = document.getElementById('provider-input');

      function formatDate(iso) {
        try {
          return new Intl.DateTimeFormat('ko-KR', {
            hour: '2-digit', minute: '2-digit', second: '2-digit',
            year: 'numeric', month: '2-digit', day: '2-digit'
          }).format(new Date(iso));
        } catch (e) {
          return iso;
        }
      }

      function escape(text) {
        const div = document.createElement('div');
        div.textContent = text || '';
        return div.innerHTML;
      }

      function renderHistory(history) {
        conversationEl.innerHTML = '';
        history.forEach((item) => {
          const card = document.createElement('div');
          card.className = 'interaction';

          const header = document.createElement('div');
          header.className = 'interaction-header';
          header.innerHTML = `<span>${formatDate(item.timestamp)}</span>`;

          const mode = document.createElement('div');
          mode.className = 'interaction-header';
          const badges = [];
          if (item.provider) {
            badges.push(`<span class=\"badge\">${escape(item.provider)}</span>`);
          }
          badges.push(`<span class=\"badge ${item.effective_offline ? 'offline' : 'execute'}\">${item.effective_offline ? '오프라인' : '온라인'}</span>`);
          badges.push(`<span class=\"badge ${item.execute ? 'execute' : 'dryrun'}\">${item.execute ? '실행' : '드라이런'}</span>`);
          mode.innerHTML = badges.join('');

          const request = document.createElement('div');
          request.className = 'request-text';
          request.innerHTML = escape(item.request);

          const summary = document.createElement('div');
          summary.className = 'summary';
          summary.innerHTML = escape(item.summary);

          card.appendChild(header);
          card.appendChild(mode);
          card.appendChild(request);
          card.appendChild(summary);

          if (item.warnings && item.warnings.length) {
            const warning = document.createElement('div');
            warning.className = 'warnings';
            warning.innerHTML = item.warnings.map(escape).join('<br/>');
            card.appendChild(warning);
          }
          if (item.error) {
            const error = document.createElement('div');
            error.className = 'error';
            error.innerHTML = escape(item.error);
            card.appendChild(error);
          }

          conversationEl.appendChild(card);
        });
        conversationEl.scrollTop = conversationEl.scrollHeight;
      }

      function renderCommands(interaction) {
        if (!interaction || !interaction.result) {
          commandEl.innerHTML = '<p style="color: var(--text-muted);">플랜 정보가 없습니다.</p>';
          return;
        }
        const result = interaction.result;
        const approved = new Set(result.safety?.approved_steps || []);
        const blocked = new Set(result.safety?.blocked_steps || []);
        commandEl.innerHTML = '';

        const planTitle = document.createElement('h3');
        planTitle.textContent = '계획 단계';
        commandEl.appendChild(planTitle);

        if (result.plan?.steps?.length) {
          result.plan.steps.forEach((step) => {
            const card = document.createElement('div');
            card.className = 'command-card';
            const status = blocked.has(step.id) ? '차단됨' : (approved.has(step.id) ? (interaction.execute ? '실행됨' : '승인됨') : '검토 중');
            card.innerHTML = `
              <h3>[${escape(step.id)}] ${escape(step.action || '')}</h3>
              <div class=\"command-meta\">
                <span>${escape(status)}</span>
                ${step.depends_on && step.depends_on.length ? `<span>depends: ${escape(step.depends_on.join(', '))}</span>` : ''}
              </div>
              <div>${escape(step.description || '')}</div>
            `;
            if (step.parameters && Object.keys(step.parameters).length) {
              const params = document.createElement('div');
              params.className = 'command-meta';
              params.textContent = 'parameters: ' + JSON.stringify(step.parameters, null, 2);
              card.appendChild(params);
            }
            commandEl.appendChild(card);
          });
        } else {
          const empty = document.createElement('p');
          empty.style.color = 'var(--text-muted)';
          empty.textContent = '생성된 계획이 없습니다.';
          commandEl.appendChild(empty);
        }

        const execTitle = document.createElement('h3');
        execTitle.style.marginTop = '12px';
        execTitle.textContent = '실행 로그';
        commandEl.appendChild(execTitle);

        if (result.execution && result.execution.length) {
          result.execution.forEach((entry) => {
            const card = document.createElement('div');
            card.className = 'command-card';
            card.innerHTML = `
              <h3>Step ${escape(entry.step_id || '')} → ${escape(entry.status || '')}</h3>
            `;
            if (entry.output) {
              const output = document.createElement('div');
              output.className = 'execution-output';
              output.textContent = entry.output;
              card.appendChild(output);
            }
            if (entry.error) {
              const error = document.createElement('div');
              error.className = 'error';
              error.textContent = entry.error;
              card.appendChild(error);
            }
            commandEl.appendChild(card);
          });
        } else {
          const empty = document.createElement('p');
          empty.style.color = 'var(--text-muted)';
          empty.textContent = interaction.execute ? '실행된 명령이 없습니다.' : '드라이런 모드입니다. 명령은 미실행 상태로 남습니다.';
          commandEl.appendChild(empty);
        }
      }

      function renderFabric(fabric) {
        fabricEl.innerHTML = '';
        const card = document.createElement('div');
        card.className = 'fabric-card';
        if (!fabric || !fabric.enabled) {
          card.innerHTML = '<strong>컨텍스트 패브릭 비활성화</strong><span style="color: var(--text-muted);">토글을 켜면 계획 컨텍스트에 최근 이벤트를 자동으로 포함합니다.</span>';
          fabricEl.appendChild(card);
          return;
        }
        card.innerHTML = `
          <strong>컨텍스트 패브릭 활성</strong>
          <span style=\"color: var(--text-muted);\">경로: ${escape(fabric.path || '기본 위치')}</span>
          <span>노드 ${fabric.metadata?.node_count ?? '-'} · 엣지 ${fabric.metadata?.edge_count ?? '-'} · 이벤트 ${fabric.metadata?.event_count ?? '-'}</span>
        `;
        if (fabric.events && fabric.events.length) {
          const list = document.createElement('div');
          list.style.display = 'flex';
          list.style.flexDirection = 'column';
          list.style.gap = '6px';
          fabric.events.forEach((event) => {
            const line = document.createElement('div');
            line.style.fontSize = '0.82rem';
            line.style.color = 'var(--text-muted)';
            line.textContent = `${formatDate(event.timestamp)} · ${event.type}`;
            list.appendChild(line);
          });
          card.appendChild(list);
        }
        fabricEl.appendChild(card);
      }

      function updateConfig(config) {
        state.config = Object.assign(state.config, config || {});
        providerBadge.textContent = config.provider ? `Provider · ${config.provider}` : 'Provider · offline';
        providerInput.value = config.provider || '';
        offlineToggle.checked = !!config.offline;
        executeToggle.checked = !!config.execute;
        fabricToggle.checked = !!config.use_fabric;
      }

      async function fetchStatus() {
        const response = await fetch('/api/status');
        if (!response.ok) return;
        const data = await response.json();
        if (!data.ok) return;
        updateConfig(data.config || {});
        state.history = data.history || [];
        state.fabric = data.fabric || null;
        renderHistory(state.history);
        renderFabric(state.fabric);
        if (state.history.length) {
          renderCommands(state.history[state.history.length - 1]);
        }
      }

      document.getElementById('prompt-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const prompt = promptInput.value.trim();
        if (!prompt) {
          promptInput.focus();
          return;
        }
        const payload = {
          prompt,
          execute: executeToggle.checked,
          offline: offlineToggle.checked,
          use_fabric: fabricToggle.checked,
          provider: providerInput.value.trim() || null
        };
        const response = await fetch('/api/orchestrate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (!data.ok) {
          alert(data.error || '오케스트레이션 실패');
          return;
        }
        if (data.config) {
          updateConfig(data.config);
        }
        if (data.interaction) {
          state.history.push(data.interaction);
          renderHistory(state.history);
          renderCommands(data.interaction);
        }
        if (data.fabric) {
          state.fabric = data.fabric;
          renderFabric(state.fabric);
        }
        promptInput.value = '';
        promptInput.focus();
      });

      fetchStatus();
    })();
  </script>
</body>
</html>
"""

INDEX_HTML_FILLED = (
    INDEX_HTML
    .replace("__LOGO_DATA_URI__", AINUX_LOGO_DATA_URI)
    .replace("__PENGUIN_DATA_URI__", AINUX_PENGUIN_DATA_URI)
)


@dataclass
class UIServerConfig:
    """Runtime configuration for the UI server."""

    host: str = "127.0.0.1"
    port: int = 8787
    provider: Optional[str] = None
    offline: bool = False
    execute: bool = False
    use_fabric: bool = True
    fabric_path: Optional[Path] = None
    fabric_event_limit: int = 20
    timeout: int = 60

    def __post_init__(self) -> None:
        if isinstance(self.fabric_path, str):
            self.fabric_path = Path(self.fabric_path).expanduser()
        self.port = int(self.port)
        self.fabric_event_limit = max(1, int(self.fabric_event_limit))
        self.timeout = int(self.timeout)


class AinuxUIServer:
    """Embeds the orchestration engine behind a small web UI."""

    def __init__(self, config: UIServerConfig) -> None:
        self._config = config
        self._state = _AinuxUIState(config)
        self._httpd: Optional[ThreadingHTTPServer] = None

    @property
    def url(self) -> str:
        host = self._config.host
        if host in {"0.0.0.0", "::"}:
            display = "localhost"
        else:
            display = host
        return f"http://{display}:{self._config.port}/"

    def serve(self, *, open_browser: bool = True) -> None:
        """Start the HTTP server and block until interrupted."""

        handler = self._build_handler()
        server = ThreadingHTTPServer((self._config.host, self._config.port), handler)
        server.daemon_threads = True
        self._httpd = server
        if open_browser:
            threading.Thread(target=self._open_browser, daemon=True).start()
        try:
            server.serve_forever()
        finally:
            server.server_close()

    def shutdown(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def _open_browser(self) -> None:
        try:
            webbrowser.open(self.url)
        except Exception:
            return

    def _build_handler(self):
        state = self._state

        class RequestHandler(BaseHTTPRequestHandler):
            """HTTP handler bound to the surrounding UI state."""

            server_version = "AinuxUI/1.0"

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler signature
                parsed = urlparse(self.path)
                if parsed.path in {"/", "/index.html"}:
                    self._send_response(HTTPStatus.OK, INDEX_HTML_FILLED, "text/html; charset=utf-8")
                    return
                if parsed.path == "/api/status":
                    payload = state.status()
                    self._send_json(payload)
                    return
                self._send_response(HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler signature
                parsed = urlparse(self.path)
                if parsed.path != "/api/orchestrate":
                    self._send_response(HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length) if length > 0 else b""
                try:
                    payload = json.loads(raw_body.decode("utf-8") or "{}") if raw_body else {}
                except json.JSONDecodeError:
                    self._send_json({"ok": False, "error": "잘못된 JSON 요청입니다."}, status=HTTPStatus.BAD_REQUEST)
                    return
                response = state.orchestrate(payload)
                status = HTTPStatus.OK if response.get("ok") else HTTPStatus.BAD_REQUEST
                self._send_json(response, status=status)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A003 - BaseHTTPRequestHandler API
                return

            def _send_json(self, payload: Dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._send_response(status, body, "application/json; charset=utf-8")

            def _send_response(self, status: HTTPStatus, body: Any, content_type: str) -> None:
                if isinstance(body, str):
                    data = body.encode("utf-8")
                else:
                    data = body
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

        return RequestHandler


class _AinuxUIState:
    """Holds mutable state for the UI server."""

    def __init__(self, config: UIServerConfig) -> None:
        self._lock = threading.Lock()
        self._config = config
        self._settings = {
            "provider": config.provider,
            "offline": config.offline,
            "execute": config.execute,
            "use_fabric": config.use_fabric,
            "fabric_event_limit": config.fabric_event_limit,
            "timeout": config.timeout,
        }
        self._fabric_path = config.fabric_path
        self._fabric = None
        if config.use_fabric:
            if self._fabric_path is None:
                self._fabric_path = default_fabric_path()
            self._fabric = load_fabric(self._fabric_path)
        self._interactions: List[Dict[str, Any]] = []
        self._counter = 0

    def status(self) -> Dict[str, Any]:
        with self._lock:
            config = dict(self._settings)
            history = list(self._interactions)[-20:]
            fabric_payload = self._fabric_payload(config)
        return {"ok": True, "config": config, "history": history, "fabric": fabric_payload}

    def orchestrate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        prompt = str(payload.get("prompt", "")).strip()
        if not prompt:
            return {"ok": False, "error": "프롬프트를 입력해주세요."}

        overrides = self._normalize_overrides(payload)
        settings = self._apply_overrides(overrides)

        provider = settings["provider"]
        offline_requested = bool(settings["offline"])
        execute = bool(settings["execute"])
        timeout = settings["timeout"]
        fabric_enabled = bool(settings["use_fabric"])
        fabric_event_limit = settings["fabric_event_limit"]

        warnings: List[str] = []
        used_offline = offline_requested
        provider_name: Optional[str] = None
        client: Optional[ChatClient] = None

        if not offline_requested:
            try:
                provider_settings = resolve_provider(provider)
            except ConfigError as exc:
                warnings.append(str(exc))
                used_offline = True
            else:
                provider_name = provider_settings.name
                client = ChatClient(provider_settings, timeout=timeout)

        fabric = self._fabric if fabric_enabled else None
        orchestrator = AinuxOrchestrator.with_client(
            client,
            fabric=fabric,
            fabric_event_limit=fabric_event_limit,
        )

        try:
            result_obj = orchestrator.orchestrate(prompt, execute=execute)
        except ChatClientError as exc:
            warnings.append(f"모델 호출에 실패하여 휴리스틱 모드로 전환했습니다: {exc}")
            used_offline = True
            orchestrator = AinuxOrchestrator.with_client(
                None,
                fabric=fabric,
                fabric_event_limit=fabric_event_limit,
            )
            try:
                result_obj = orchestrator.orchestrate(prompt, execute=execute)
            except OrchestrationError as inner_exc:
                return {
                    "ok": False,
                    "error": str(inner_exc),
                    "warnings": warnings,
                    "config": settings,
                }
        except OrchestrationError as exc:
            interaction = self._record_interaction(
                prompt,
                None,
                warnings,
                error=str(exc),
                provider=provider_name,
                execute=execute,
                effective_offline=used_offline,
            )
            return {
                "ok": False,
                "error": str(exc),
                "warnings": warnings,
                "config": settings,
                "interaction": interaction,
                "fabric": self._fabric_payload(settings),
            }

        result_payload = _result_to_dict(result_obj)
        interaction = self._record_interaction(
            prompt,
            result_payload,
            warnings,
            provider=provider_name,
            execute=execute,
            effective_offline=used_offline,
        )

        return {
            "ok": True,
            "interaction": interaction,
            "config": settings,
            "fabric": self._fabric_payload(settings),
        }

    def _record_interaction(
        self,
        prompt: str,
        result: Optional[Dict[str, Any]],
        warnings: List[str],
        *,
        provider: Optional[str],
        execute: bool,
        effective_offline: bool,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self._counter += 1
            interaction = {
                "id": f"ux-{self._counter:04d}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "request": prompt,
                "result": result,
                "summary": _summarize_result(result, execute) if result else "실패한 요청입니다.",
                "warnings": list(warnings),
                "provider": provider,
                "execute": execute,
                "effective_offline": effective_offline,
                "error": error,
            }
            if result:
                interaction["result"] = result
            fabric_meta = None
            if result:
                fabric_meta = self._save_fabric()
            interaction["fabric"] = fabric_meta
            self._interactions.append(interaction)
            return interaction

    def _save_fabric(self) -> Optional[Dict[str, Any]]:
        if not self._fabric or not self._settings.get("use_fabric"):
            return None
        if self._fabric_path is None:
            self._fabric_path = default_fabric_path()
        snapshot = self._fabric.snapshot(event_limit=self._settings.get("fabric_event_limit", 20))
        saved_path = self._fabric.save(self._fabric_path)
        return {
            "enabled": True,
            "path": str(saved_path),
            "metadata": snapshot.metadata,
            "events": [event.to_dict() for event in snapshot.events],
        }

    def _fabric_payload(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        enabled = bool(settings.get("use_fabric"))
        if not enabled:
            return {"enabled": False, "path": str(self._fabric_path) if self._fabric_path else None}
        if not self._fabric:
            return {"enabled": True, "path": str(self._fabric_path) if self._fabric_path else None, "metadata": {}, "events": []}
        snapshot = self._fabric.snapshot(event_limit=settings.get("fabric_event_limit", 20))
        return {
            "enabled": True,
            "path": str(self._fabric_path) if self._fabric_path else None,
            "metadata": snapshot.metadata,
            "events": [event.to_dict() for event in snapshot.events],
        }

    def _normalize_overrides(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        overrides: Dict[str, Any] = {}
        if "provider" in payload:
            provider = payload.get("provider")
            overrides["provider"] = str(provider).strip() or None if provider is not None else None
        if "offline" in payload:
            overrides["offline"] = _coerce_bool(payload.get("offline"))
        if "execute" in payload:
            overrides["execute"] = _coerce_bool(payload.get("execute"))
        if "use_fabric" in payload:
            overrides["use_fabric"] = _coerce_bool(payload.get("use_fabric"))
        if "timeout" in payload:
            overrides["timeout"] = _coerce_int(payload.get("timeout"))
        if "fabric_event_limit" in payload:
            overrides["fabric_event_limit"] = _coerce_int(payload.get("fabric_event_limit"))
        return overrides

    def _apply_overrides(self, overrides: Dict[str, Any]) -> Dict[str, Any]:
        reload_fabric = False
        with self._lock:
            settings = dict(self._settings)
            if "provider" in overrides:
                settings["provider"] = overrides["provider"]
            if "offline" in overrides and overrides["offline"] is not None:
                settings["offline"] = bool(overrides["offline"])
            if "execute" in overrides and overrides["execute"] is not None:
                settings["execute"] = bool(overrides["execute"])
            if "use_fabric" in overrides and overrides["use_fabric"] is not None:
                new_value = bool(overrides["use_fabric"])
                if new_value != settings.get("use_fabric"):
                    reload_fabric = new_value
                settings["use_fabric"] = new_value
            if "timeout" in overrides and overrides["timeout"]:
                settings["timeout"] = max(1, int(overrides["timeout"]))
            if "fabric_event_limit" in overrides and overrides["fabric_event_limit"]:
                settings["fabric_event_limit"] = max(1, int(overrides["fabric_event_limit"]))
            self._settings = settings
        if reload_fabric:
            if self._fabric_path is None:
                self._fabric_path = self._config.fabric_path or default_fabric_path()
            self._fabric = load_fabric(self._fabric_path)
        return dict(self._settings)


def _coerce_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "t", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "f", "no", "n", "off"}:
            return False
        return None
    return bool(value)


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _result_to_dict(result: Any) -> Dict[str, Any]:
    if result is None:
        return {}
    return {
        "intent": {
            "raw_input": result.intent.raw_input,
            "action": result.intent.action,
            "confidence": result.intent.confidence,
            "parameters": result.intent.parameters,
            "reasoning": result.intent.reasoning,
        },
        "plan": {
            "notes": result.plan.notes,
            "steps": [
                {
                    "id": step.id,
                    "action": step.action,
                    "description": step.description,
                    "parameters": step.parameters,
                    "depends_on": step.depends_on,
                }
                for step in result.plan.steps
            ],
        },
        "safety": {
            "approved_steps": [step.id for step in result.safety.approved_steps],
            "blocked_steps": [step.id for step in result.safety.blocked_steps],
            "warnings": result.safety.warnings,
            "rationale": result.safety.rationale,
        },
        "execution": [
            {
                "step_id": entry.step_id,
                "status": entry.status,
                "output": entry.output,
                "error": entry.error,
            }
            for entry in result.execution
        ],
    }


def _summarize_result(result: Optional[Dict[str, Any]], execute: bool) -> str:
    if not result:
        return "결과 정보가 없습니다."
    intent = result.get("intent", {})
    action = intent.get("action") or "의도를 파악할 수 없습니다"
    confidence = intent.get("confidence")
    if confidence is None:
        confidence_text = "?"
    else:
        try:
            confidence_text = f"{float(confidence):.2f}"
        except (TypeError, ValueError):
            confidence_text = "?"
    plan = result.get("plan", {})
    steps = plan.get("steps", [])
    safety = result.get("safety", {})
    approved = safety.get("approved_steps", [])
    blocked = safety.get("blocked_steps", [])
    execution = result.get("execution", [])
    lines = [
        f"의도: {action} (신뢰도 {confidence_text})",
        f"계획 단계 {len(steps)}개 · 승인 {len(approved)}개 · 차단 {len(blocked)}개",
    ]
    if execute:
        lines.append(f"실행된 명령 {len(execution)}개")
    else:
        lines.append("드라이런 모드 – 명령은 실행하지 않았습니다")
    if steps:
        highlight = steps[0]
        summary = highlight.get("description") or highlight.get("action")
        if summary:
            lines.append(f"첫 단계: {summary}")
    if safety.get("warnings"):
        lines.append(f"경고 {len(safety['warnings'])}건")
    return "\n".join(lines)


__all__ = ["AinuxUIServer", "UIServerConfig"]
