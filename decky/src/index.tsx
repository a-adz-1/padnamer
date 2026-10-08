// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 the padnamer authors
import {
  ButtonItem,
  DialogButton,
  Field,
  Focusable,
  Navigation,
  PanelSection,
  PanelSectionRow,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin, toaster } from "@decky/api";
import { useEffect, useState } from "react";
import { FaGamepad } from "react-icons/fa";

type Indicator = { pattern: string; colour: string };
type Controller = {
  id: string;
  name: string;
  model_label: string;
  connected: boolean;
  bluetooth: boolean;
  player: number | null;
  indicator: Indicator;
  capabilities: { player_leds: number; lightbar: boolean; rumble: boolean };
};
type State = {
  error?: string;
  session: { emulator: string; systems: string[] } | null;
  manual: { pending: boolean; scope: string } | null;
  players: string[];
  controllers: Controller[];
  systems: { id: string; label: string }[];
};
type Result = { error?: string };

const getState = callable<[], State>("get_state");
const move = callable<[cid: string, delta: number, always: boolean], Result>("move");
const identify = callable<[cid: string], Result>("identify");
const reset = callable<[], Result>("reset");
const disconnect = callable<[cid: string], Result>("disconnect");
const disconnectAll = callable<[], Result>("disconnect_all");

const COLOURS = ["#3d6cff", "#e8403f", "#1fa85a", "#c63fc0", "#f08a1c", "#11a7b8", "#c9a800", "#7a8399"];

function Lights({ c }: { c: Controller }) {
  const ind = c.indicator || { pattern: "", colour: "" };
  if (!ind.pattern && !ind.colour) {
    return <span style={{ opacity: 0.7 }}>{c.capabilities?.rumble ? "No player lights – use Find" : "No player lights"}</span>;
  }
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: "5px" }}>
      {[...(ind.pattern || "")].map((ch, i) => (
        <span key={i} style={{
          width: "9px", height: "9px", borderRadius: "50%",
          background: ch === "●" ? "#46e07a" : "#4a5370",
          boxShadow: ch === "●" ? "0 0 5px #46e07a" : "none",
        }} />
      ))}
      {ind.colour && <span style={{ width: "26px", height: "6px", borderRadius: "3px", background: ind.colour }} />}
    </span>
  );
}

function Content() {
  const [state, setState] = useState<State | null>(null);
  const [always, setAlways] = useState(false);
  const [busy, setBusy] = useState(false);

  const refresh = async () => {
    try {
      setState(await getState());
    } catch (e) {
      setState({ error: String(e) } as State);
    }
  };

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, []);

  const run = async (fn: () => Promise<Result>, done?: string) => {
    if (busy) return;
    setBusy(true);
    try {
      const r = await fn();
      if (r && r.error) toaster.toast({ title: "Controllers", body: r.error });
      else if (done) toaster.toast({ title: "Controllers", body: done });
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  if (!state) return <PanelSection><PanelSectionRow>Loading…</PanelSectionRow></PanelSection>;
  if (state.error) {
    return (
      <PanelSection title="Controllers">
        <PanelSectionRow>
          <Field label="padnamer isn't reachable" description="Check the padnamer service is installed and running." />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const byId = new Map(state.controllers.map((c) => [c.id, c]));
  const players = state.players.map((id) => byId.get(id)).filter(Boolean) as Controller[];
  const label = (id: string) => state.systems.find((s) => s.id === id)?.label || id;
  const s = state.session;
  const m = state.manual;

  let note = "Each game puts its own controllers first.";
  if (always) note = "Moves change the usual order.";
  else if (m?.pending) note = "Set by hand for the next game.";
  else if (m) note = m.scope === "persistent" ? "Set by hand until reset." : "Set by hand for this game.";

  return (
    <>
      <PanelSection title={s ? `Playing ${s.systems.map(label).join(" / ") || "a game"}` : "Players"}>
        <PanelSectionRow>
          <div style={{ fontSize: "12px", opacity: 0.8, marginBottom: "4px" }}>{note}</div>
        </PanelSectionRow>
        {players.length === 0 && (
          <PanelSectionRow>
            <Field label="No controllers connected" description="Turn one on, or pair it in Settings → Bluetooth." />
          </PanelSectionRow>
        )}
        {players.map((c, i) => (
          <PanelSectionRow key={c.id}>
            <Field
              label={
                <span style={{ display: "inline-flex", alignItems: "center", gap: "8px" }}>
                  <span style={{
                    background: COLOURS[((c.player || i + 1) - 1) % 8], color: "#fff", borderRadius: "6px",
                    minWidth: "24px", textAlign: "center", fontWeight: 700,
                  }}>{c.player || i + 1}</span>
                  {c.name}
                </span>
              }
              description={<Lights c={c} />}
              bottomSeparator="none"
            />
            <Focusable style={{ display: "flex", gap: "6px", marginBottom: "10px" }} flow-children="horizontal">
              <DialogButton style={{ minWidth: 0, flex: 1 }} disabled={busy || i === 0}
                onClick={() => run(() => move(c.id, -1, always))}>▲</DialogButton>
              <DialogButton style={{ minWidth: 0, flex: 1 }} disabled={busy || i === players.length - 1}
                onClick={() => run(() => move(c.id, 1, always))}>▼</DialogButton>
              <DialogButton style={{ minWidth: 0, flex: 1.4 }} disabled={busy}
                onClick={() => run(() => identify(c.id), `Look for ${c.name} flashing`)}>Find</DialogButton>
              {c.bluetooth && (
                <DialogButton style={{ minWidth: 0, flex: 1.2 }} disabled={busy}
                  onClick={() => run(() => disconnect(c.id), `${c.name} disconnected`)}>Off</DialogButton>
              )}
            </Focusable>
          </PanelSectionRow>
        ))}
        <PanelSectionRow>
          <ToggleField label="Save as the usual order" checked={always} onChange={setAlways} />
        </PanelSectionRow>
        {m && (
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={busy} onClick={() => run(reset, "Back to automatic order")}>
              Back to automatic order
            </ButtonItem>
          </PanelSectionRow>
        )}
        {players.some((c) => c.bluetooth) && (
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={busy} onClick={() => run(disconnectAll, "Disconnecting all controllers")}>
              Disconnect all controllers
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => {
            Navigation.CloseSideMenus();
            Navigation.NavigateToExternalWeb("http://127.0.0.1:8742/?kiosk=1");
          }}>
            Edit controllers
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => ({
  name: "padnamer",
  titleView: <div className={staticClasses.Title}>Controllers</div>,
  content: <Content />,
  icon: <FaGamepad />,
  onDismount() {},
}));
