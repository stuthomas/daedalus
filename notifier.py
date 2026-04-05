"""
Daedalus Notifier
Sends styled HTML email reports after each agent cycle via Gmail SMTP.

Setup:
  1. Enable 2FA on your Gmail account
  2. Create an App Password: Google Account → Security → App Passwords
  3. Set SMTP_USER, SMTP_PASS, NOTIFY_EMAIL in your .env
"""

import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytz

log = logging.getLogger("daedalus.notifier")
AEST = pytz.timezone("Australia/Sydney")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _send(config, subject: str, html: str) -> None:
    if not all([config.NOTIFY_EMAIL, config.SMTP_USER, config.SMTP_PASS]):
        log.warning("Email not fully configured — skipping notification")
        return

    msg = MIMEMultipart("alternative")
    msg["From"]    = f"Daedalus Portfolio <{config.SMTP_USER}>"
    msg["To"]      = config.NOTIFY_EMAIL
    msg["Subject"] = f"[Daedalus] {subject}"
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(config.SMTP_USER, config.SMTP_PASS)
        server.send_message(msg)

    log.info(f"Email sent to {config.NOTIFY_EMAIL}: {subject}")


def _colour(condition: bool, true_c="#10b981", false_c="#ef4444") -> str:
    return true_c if condition else false_c


# ── Cycle Summary Email ───────────────────────────────────────────────────────

def send_cycle_summary(
    config,
    portfolio: dict,
    analyst: dict | None,
    news: dict | None,
    pm: dict | None,
    executed: list,
    pending: list,
    errors: list,
) -> None:
    now       = datetime.now(AEST).strftime("%A %d %b %Y, %H:%M AEST")
    invested  = sum(h["shares"] * h.get("currentPrice", h["avgBuyPrice"]) for h in portfolio.get("holdings", []))
    total     = portfolio["cash"] + invested
    pl_abs    = total - portfolio["startingCapital"]
    pl_pct    = (pl_abs / portfolio["startingCapital"]) * 100
    pl_colour = _colour(pl_abs >= 0)

    sentiment       = (news or {}).get("sentiment", "—")
    sentiment_score = (news or {}).get("score", "—")
    macro           = (news or {}).get("macro", "")
    sentiment_c     = {"BULLISH": "#10b981", "BEARISH": "#ef4444"}.get(sentiment, "#f59e0b")

    # ── Holdings rows ──────────────────────────────────────────────────────
    holdings_rows = ""
    for h in portfolio.get("holdings", []):
        cur     = h.get("currentPrice", h["avgBuyPrice"])
        h_pl    = ((cur - h["avgBuyPrice"]) / h["avgBuyPrice"]) * 100
        h_c     = _colour(h_pl >= 0)
        val     = h["shares"] * cur
        holdings_rows += f"""
        <tr style="border-bottom:1px solid #e2e8f0">
          <td style="padding:8px 12px;font-family:monospace;font-weight:700;color:#0d1530">{h['ticker']}</td>
          <td style="padding:8px 12px;color:#475569">{h['name']}</td>
          <td style="padding:8px 12px;font-family:monospace">{h['shares']}</td>
          <td style="padding:8px 12px;font-family:monospace">${cur:.2f}</td>
          <td style="padding:8px 12px;font-family:monospace;font-weight:700">${val:.2f}</td>
          <td style="padding:8px 12px;font-family:monospace;color:{h_c};font-weight:700">{'+' if h_pl>=0 else ''}{h_pl:.2f}%</td>
        </tr>"""

    if not holdings_rows:
        holdings_rows = '<tr><td colspan="6" style="padding:12px;color:#94a3b8;font-style:italic;text-align:center">No current positions</td></tr>'

    # ── Trade rows ─────────────────────────────────────────────────────────
    trade_blocks = ""
    for t in executed:
        trade_blocks += f"""
        <div style="background:#f0fdf4;border-left:4px solid #10b981;padding:12px 14px;margin:6px 0;border-radius:0 6px 6px 0">
          <div style="font-size:12px;font-weight:700;color:#166534;margin-bottom:4px">✓ EXECUTED</div>
          <div style="font-size:14px;font-weight:700;color:#0d1530">{t['action']} {t['shares']}× <span style="font-family:monospace">{t['ticker']}</span> @ ${t['price']:.2f} <span style="color:#475569">= ${t['total']:.2f} AUD</span></div>
          <div style="font-size:12px;color:#475569;margin-top:4px">{t.get('reason','')}</div>
        </div>"""

    for t in pending:
        trade_blocks += f"""
        <div style="background:#fffbeb;border-left:4px solid #f59e0b;padding:12px 14px;margin:6px 0;border-radius:0 6px 6px 0">
          <div style="font-size:12px;font-weight:700;color:#92400e;margin-bottom:4px">⏳ PENDING — approve in dashboard</div>
          <div style="font-size:14px;font-weight:700;color:#0d1530">{t['action']} {t['shares']}× <span style="font-family:monospace">{t['ticker']}</span> @ ${t['price']:.2f} <span style="color:#475569">= ${t['total']:.2f} AUD</span></div>
          <div style="font-size:12px;color:#475569;margin-top:4px">{t.get('reason','')}</div>
        </div>"""

    if not trade_blocks:
        trade_blocks = '<p style="color:#94a3b8;font-style:italic;font-size:13px;margin:8px 0">No trades this cycle — holding current positions</p>'

    # ── Analyst recs ────────────────────────────────────────────────────────
    rec_rows = ""
    for r in (analyst or {}).get("recs", []):
        ac = {"BUY": "#10b981", "HOLD": "#f59e0b", "AVOID": "#ef4444"}.get(r.get("action",""), "#94a3b8")
        rec_rows += f"""
        <tr style="border-bottom:1px solid #e2e8f0">
          <td style="padding:7px 12px;font-family:monospace;font-weight:700;color:#0d1530">{r['t']}</td>
          <td style="padding:7px 12px;color:#475569">{r['n']}</td>
          <td style="padding:7px 12px"><span style="color:{ac};font-weight:700;font-size:12px">{r.get('action','')}</span></td>
          <td style="padding:7px 12px;font-family:monospace">${r.get('price',0):.2f}</td>
          <td style="padding:7px 12px;color:#64748b;font-size:12px">{r.get('conf','')}</td>
          <td style="padding:7px 12px;color:#64748b;font-size:11px">{r.get('thesis','')[:60]}{'…' if len(r.get('thesis',''))>60 else ''}</td>
        </tr>"""

    analyst_section = f"""
      <div style="padding:20px 28px;border-bottom:1px solid #e2e8f0">
        <div style="font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:12px">Corporate Analyst Recommendations</div>
        <table style="width:100%;border-collapse:collapse">
          <tr style="background:#f8fafc">
            <th style="padding:7px 12px;text-align:left;font-size:10px;color:#64748b;text-transform:uppercase">Ticker</th>
            <th style="padding:7px 12px;text-align:left;font-size:10px;color:#64748b;text-transform:uppercase">Name</th>
            <th style="padding:7px 12px;text-align:left;font-size:10px;color:#64748b;text-transform:uppercase">Action</th>
            <th style="padding:7px 12px;text-align:left;font-size:10px;color:#64748b;text-transform:uppercase">Price</th>
            <th style="padding:7px 12px;text-align:left;font-size:10px;color:#64748b;text-transform:uppercase">Confidence</th>
            <th style="padding:7px 12px;text-align:left;font-size:10px;color:#64748b;text-transform:uppercase">Thesis</th>
          </tr>
          {rec_rows or '<tr><td colspan="6" style="padding:12px;color:#94a3b8;font-style:italic">No recommendations</td></tr>'}
        </table>
        {f'<p style="font-size:12px;color:#475569;margin-top:10px;line-height:1.6">{analyst.get("market","")}</p>' if analyst else ''}
      </div>""" if rec_rows else ""

    # ── Error block ─────────────────────────────────────────────────────────
    error_block = ""
    if errors:
        error_list = "".join(f"<li style='margin:4px 0'>{e}</li>" for e in errors)
        error_block = f"""
      <div style="padding:14px 28px;background:#fef2f2;border-top:1px solid #fecaca">
        <div style="font-size:11px;font-weight:700;color:#dc2626;margin-bottom:6px">⚠ Cycle completed with errors</div>
        <ul style="font-size:12px;color:#dc2626;margin:0;padding-left:18px">{error_list}</ul>
      </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Daedalus Cycle Report</title></head>
<body style="margin:0;padding:20px;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif">
<div style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.10)">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#060d1e 0%,#0d1a40 100%);padding:28px 28px 22px">
    <div style="display:flex;justify-content:space-between;align-items:flex-start">
      <div>
        <div style="color:#00d4ff;font-size:10px;letter-spacing:2.5px;text-transform:uppercase;margin-bottom:8px">Daedalus · ASX Portfolio AI</div>
        <div style="color:#f1f5f9;font-size:22px;font-weight:700;line-height:1.2">Agent Cycle Report</div>
        <div style="color:#475569;font-size:12px;margin-top:5px">{now}</div>
      </div>
      <div style="text-align:right">
        <div style="color:{sentiment_c};font-size:13px;font-weight:700">{sentiment}</div>
        <div style="color:{sentiment_c};font-family:monospace;font-size:18px">{sentiment_score}/100</div>
      </div>
    </div>
  </div>

  <!-- Portfolio Stats -->
  <div style="display:flex;padding:20px 28px;background:#f8fafc;border-bottom:1px solid #e2e8f0;gap:0">
    {_stat_block('Total Value', f'${total:.2f} AUD', None)}
    {_stat_block('Total P&L', f"{'+'if pl_abs>=0 else ''}{pl_pct:.2f}%", f"{'+'if pl_abs>=0 else ''}${pl_abs:.2f}", pl_colour)}
    {_stat_block('Cash', f'${portfolio["cash"]:.2f}', f'{portfolio["cash"]/portfolio["startingCapital"]*100:.0f}% of capital', None)}
    {_stat_block('Holdings', str(len(portfolio.get("holdings",[]))), f'${invested:.2f} invested', None)}
  </div>

  <!-- Trades -->
  <div style="padding:20px 28px;border-bottom:1px solid #e2e8f0">
    <div style="font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:12px">Trade Actions This Cycle</div>
    {trade_blocks}
  </div>

  <!-- Holdings -->
  <div style="padding:20px 28px;border-bottom:1px solid #e2e8f0">
    <div style="font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:12px">Current Holdings</div>
    <table style="width:100%;border-collapse:collapse">
      <tr style="background:#f8fafc">
        <th style="padding:7px 12px;text-align:left;font-size:10px;color:#64748b;text-transform:uppercase">Ticker</th>
        <th style="padding:7px 12px;text-align:left;font-size:10px;color:#64748b;text-transform:uppercase">Name</th>
        <th style="padding:7px 12px;text-align:left;font-size:10px;color:#64748b;text-transform:uppercase">Shares</th>
        <th style="padding:7px 12px;text-align:left;font-size:10px;color:#64748b;text-transform:uppercase">Price</th>
        <th style="padding:7px 12px;text-align:left;font-size:10px;color:#64748b;text-transform:uppercase">Value</th>
        <th style="padding:7px 12px;text-align:left;font-size:10px;color:#64748b;text-transform:uppercase">Return</th>
      </tr>
      {holdings_rows}
    </table>
  </div>

  {analyst_section}

  <!-- Macro -->
  {f'<div style="padding:20px 28px;border-bottom:1px solid #e2e8f0"><div style="font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:10px">Macro Outlook</div><p style="font-size:13px;color:#374151;line-height:1.65;margin:0">{macro}</p></div>' if macro else ''}

  {error_block}

  <!-- Footer -->
  <div style="padding:16px 28px;background:#f8fafc">
    <p style="font-size:11px;color:#94a3b8;text-align:center;margin:0;line-height:1.6">
      Daedalus · Paper Trading Simulation · Not Financial Advice<br>
      All trades are simulated. Do not rely on this for real investment decisions.
    </p>
  </div>

</div>
</body></html>"""

    sign = "+" if pl_abs >= 0 else ""
    _send(config, f"Cycle Report — ${total:.2f} AUD ({sign}{pl_pct:.1f}%)", html)

def _stat_block(label: str, value: str, sub: str | None = None, colour: str | None = None) -> str:
    val_style = f"color:{colour};" if colour else ""
    sub_html  = f'<div style="font-size:11px;color:#64748b;margin-top:2px">{sub}</div>' if sub else ""
    return f"""<div style="flex:1;padding:0 12px;border-right:1px solid #e2e8f0">
      <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">{label}</div>
      <div style="font-size:16px;font-weight:700;font-family:monospace;{val_style}">{value}</div>
      {sub_html}
    </div>"""
