const manifest = {"name":"padnamer"};
const API_VERSION = 2;
const internalAPIConnection = window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit;
if (!internalAPIConnection) {
    throw new Error('[@decky/api]: Failed to connect to the loader as as the loader API was not initialized. This is likely a bug in Decky Loader.');
}
let api;
try {
    api = internalAPIConnection.connect(API_VERSION, manifest.name);
}
catch {
    api = internalAPIConnection.connect(1, manifest.name);
    console.warn(`[@decky/api] Requested API version ${API_VERSION} but the running loader only supports version 1. Some features may not work.`);
}
if (api._version != API_VERSION) {
    console.warn(`[@decky/api] Requested API version ${API_VERSION} but the running loader only supports version ${api._version}. Some features may not work.`);
}
const callable = api.callable;
const toaster = api.toaster;
const definePlugin = (fn) => {
    return (...args) => {
        return fn(...args);
    };
};

var DefaultContext = {
  color: undefined,
  size: undefined,
  className: undefined,
  style: undefined,
  attr: undefined
};
var IconContext = SP_REACT.createContext && /*#__PURE__*/SP_REACT.createContext(DefaultContext);

var _excluded = ["attr", "size", "title"];
function _objectWithoutProperties(e, t) { if (null == e) return {}; var o, r, i = _objectWithoutPropertiesLoose(e, t); if (Object.getOwnPropertySymbols) { var n = Object.getOwnPropertySymbols(e); for (r = 0; r < n.length; r++) o = n[r], -1 === t.indexOf(o) && {}.propertyIsEnumerable.call(e, o) && (i[o] = e[o]); } return i; }
function _objectWithoutPropertiesLoose(r, e) { if (null == r) return {}; var t = {}; for (var n in r) if ({}.hasOwnProperty.call(r, n)) { if (-1 !== e.indexOf(n)) continue; t[n] = r[n]; } return t; }
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function ownKeys(e, r) { var t = Object.keys(e); if (Object.getOwnPropertySymbols) { var o = Object.getOwnPropertySymbols(e); r && (o = o.filter(function (r) { return Object.getOwnPropertyDescriptor(e, r).enumerable; })), t.push.apply(t, o); } return t; }
function _objectSpread(e) { for (var r = 1; r < arguments.length; r++) { var t = null != arguments[r] ? arguments[r] : {}; r % 2 ? ownKeys(Object(t), true).forEach(function (r) { _defineProperty(e, r, t[r]); }) : Object.getOwnPropertyDescriptors ? Object.defineProperties(e, Object.getOwnPropertyDescriptors(t)) : ownKeys(Object(t)).forEach(function (r) { Object.defineProperty(e, r, Object.getOwnPropertyDescriptor(t, r)); }); } return e; }
function _defineProperty(e, r, t) { return (r = _toPropertyKey(r)) in e ? Object.defineProperty(e, r, { value: t, enumerable: true, configurable: true, writable: true }) : e[r] = t, e; }
function _toPropertyKey(t) { var i = _toPrimitive(t, "string"); return "symbol" == typeof i ? i : i + ""; }
function _toPrimitive(t, r) { if ("object" != typeof t || !t) return t; var e = t[Symbol.toPrimitive]; if (void 0 !== e) { var i = e.call(t, r); if ("object" != typeof i) return i; throw new TypeError("@@toPrimitive must return a primitive value."); } return ("string" === r ? String : Number)(t); }
function Tree2Element(tree) {
  return tree && tree.map((node, i) => /*#__PURE__*/SP_REACT.createElement(node.tag, _objectSpread({
    key: i
  }, node.attr), Tree2Element(node.child)));
}
function GenIcon(data) {
  return props => /*#__PURE__*/SP_REACT.createElement(IconBase, _extends({
    attr: _objectSpread({}, data.attr)
  }, props), Tree2Element(data.child));
}
function IconBase(props) {
  var elem = conf => {
    var attr = props.attr,
      size = props.size,
      title = props.title,
      svgProps = _objectWithoutProperties(props, _excluded);
    var computedSize = size || conf.size || "1em";
    var className;
    if (conf.className) className = conf.className;
    if (props.className) className = (className ? className + " " : "") + props.className;
    return /*#__PURE__*/SP_REACT.createElement("svg", _extends({
      stroke: "currentColor",
      fill: "currentColor",
      strokeWidth: "0"
    }, conf.attr, attr, svgProps, {
      className: className,
      style: _objectSpread(_objectSpread({
        color: props.color || conf.color
      }, conf.style), props.style),
      height: computedSize,
      width: computedSize,
      xmlns: "http://www.w3.org/2000/svg"
    }), title && /*#__PURE__*/SP_REACT.createElement("title", null, title), props.children);
  };
  return IconContext !== undefined ? /*#__PURE__*/SP_REACT.createElement(IconContext.Consumer, null, conf => elem(conf)) : elem(DefaultContext);
}

// THIS FILE IS AUTO GENERATED
function FaGamepad (props) {
  return GenIcon({"attr":{"viewBox":"0 0 640 512"},"child":[{"tag":"path","attr":{"d":"M480.07 96H160a160 160 0 1 0 114.24 272h91.52A160 160 0 1 0 480.07 96zM248 268a12 12 0 0 1-12 12h-52v52a12 12 0 0 1-12 12h-24a12 12 0 0 1-12-12v-52H84a12 12 0 0 1-12-12v-24a12 12 0 0 1 12-12h52v-52a12 12 0 0 1 12-12h24a12 12 0 0 1 12 12v52h52a12 12 0 0 1 12 12zm216 76a40 40 0 1 1 40-40 40 40 0 0 1-40 40zm64-96a40 40 0 1 1 40-40 40 40 0 0 1-40 40z"},"child":[]}]})(props);
}

const getState = callable("get_state");
const move = callable("move");
const identify = callable("identify");
const reset = callable("reset");
const disconnect = callable("disconnect");
const disconnectAll = callable("disconnect_all");
const COLOURS = ["#3d6cff", "#e8403f", "#1fa85a", "#c63fc0", "#f08a1c", "#11a7b8", "#c9a800", "#7a8399"];
function Lights({ c }) {
    const ind = c.indicator || { pattern: "", colour: "" };
    if (!ind.pattern && !ind.colour) {
        return SP_JSX.jsx("span", { style: { opacity: 0.7 }, children: c.capabilities?.rumble ? "No player lights – use Find" : "No player lights" });
    }
    return (SP_JSX.jsxs("span", { style: { display: "inline-flex", alignItems: "center", gap: "5px" }, children: [[...(ind.pattern || "")].map((ch, i) => (SP_JSX.jsx("span", { style: {
                    width: "9px", height: "9px", borderRadius: "50%",
                    background: ch === "●" ? "#46e07a" : "#4a5370",
                    boxShadow: ch === "●" ? "0 0 5px #46e07a" : "none",
                } }, i))), ind.colour && SP_JSX.jsx("span", { style: { width: "26px", height: "6px", borderRadius: "3px", background: ind.colour } })] }));
}
function Content() {
    const [state, setState] = SP_REACT.useState(null);
    const [always, setAlways] = SP_REACT.useState(false);
    const [busy, setBusy] = SP_REACT.useState(false);
    const refresh = async () => {
        try {
            setState(await getState());
        }
        catch (e) {
            setState({ error: String(e) });
        }
    };
    SP_REACT.useEffect(() => {
        refresh();
        const t = setInterval(refresh, 2000);
        return () => clearInterval(t);
    }, []);
    const run = async (fn, done) => {
        if (busy)
            return;
        setBusy(true);
        try {
            const r = await fn();
            if (r && r.error)
                toaster.toast({ title: "Controllers", body: r.error });
            else if (done)
                toaster.toast({ title: "Controllers", body: done });
            await refresh();
        }
        finally {
            setBusy(false);
        }
    };
    if (!state)
        return SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: "Loading\u2026" }) });
    if (state.error) {
        return (SP_JSX.jsx(DFL.PanelSection, { title: "Controllers", children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.Field, { label: "padnamer isn't reachable", description: "Check the padnamer service is installed and running." }) }) }));
    }
    const byId = new Map(state.controllers.map((c) => [c.id, c]));
    const players = state.players.map((id) => byId.get(id)).filter(Boolean);
    const label = (id) => state.systems.find((s) => s.id === id)?.label || id;
    const s = state.session;
    const m = state.manual;
    let note = "Each game puts its own controllers first.";
    if (always)
        note = "Moves change the usual order.";
    else if (m?.pending)
        note = "Set by hand for the next game.";
    else if (m)
        note = m.scope === "persistent" ? "Set by hand until reset." : "Set by hand for this game.";
    return (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsxs(DFL.PanelSection, { title: s ? `Playing ${s.systems.map(label).join(" / ") || "a game"}` : "Players", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: "12px", opacity: 0.8, marginBottom: "4px" }, children: note }) }), players.length === 0 && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.Field, { label: "No controllers connected", description: "Turn one on, or pair it in Settings \u2192 Bluetooth." }) })), players.map((c, i) => (SP_JSX.jsxs(DFL.PanelSectionRow, { children: [SP_JSX.jsx(DFL.Field, { label: SP_JSX.jsxs("span", { style: { display: "inline-flex", alignItems: "center", gap: "8px" }, children: [SP_JSX.jsx("span", { style: {
                                                background: COLOURS[((c.player || i + 1) - 1) % 8], color: "#fff", borderRadius: "6px",
                                                minWidth: "24px", textAlign: "center", fontWeight: 700,
                                            }, children: c.player || i + 1 }), c.name] }), description: SP_JSX.jsx(Lights, { c: c }), bottomSeparator: "none" }), SP_JSX.jsxs(DFL.Focusable, { style: { display: "flex", gap: "6px", marginBottom: "10px" }, "flow-children": "horizontal", children: [SP_JSX.jsx(DFL.DialogButton, { style: { minWidth: 0, flex: 1 }, disabled: busy || i === 0, onClick: () => run(() => move(c.id, -1, always)), children: "\u25B2" }), SP_JSX.jsx(DFL.DialogButton, { style: { minWidth: 0, flex: 1 }, disabled: busy || i === players.length - 1, onClick: () => run(() => move(c.id, 1, always)), children: "\u25BC" }), SP_JSX.jsx(DFL.DialogButton, { style: { minWidth: 0, flex: 1.4 }, disabled: busy, onClick: () => run(() => identify(c.id), `Look for ${c.name} flashing`), children: "Find" }), c.bluetooth && (SP_JSX.jsx(DFL.DialogButton, { style: { minWidth: 0, flex: 1.2 }, disabled: busy, onClick: () => run(() => disconnect(c.id), `${c.name} disconnected`), children: "Off" }))] })] }, c.id))), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ToggleField, { label: "Save as the usual order", checked: always, onChange: setAlways }) }), m && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run(reset, "Back to automatic order"), children: "Back to automatic order" }) })), players.some((c) => c.bluetooth) && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run(disconnectAll, "Disconnecting all controllers"), children: "Disconnect all controllers" }) }))] }), SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => {
                            DFL.Navigation.CloseSideMenus();
                            DFL.Navigation.NavigateToExternalWeb("http://127.0.0.1:8742/?kiosk=1");
                        }, children: "Edit controllers" }) }) })] }));
}
var index = definePlugin(() => ({
    name: "padnamer",
    titleView: SP_JSX.jsx("div", { className: DFL.staticClasses.Title, children: "Controllers" }),
    content: SP_JSX.jsx(Content, {}),
    icon: SP_JSX.jsx(FaGamepad, {}),
    onDismount() { },
}));

export { index as default };
//# sourceMappingURL=index.js.map
