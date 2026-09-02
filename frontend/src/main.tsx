import "antd/dist/reset.css";
import { App as AntApp, ConfigProvider, theme } from "antd";
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { ConversationLayoutPreview } from "./preview/ConversationLayoutPreview";
import { RunStatesPreview } from "./preview/RunStatesPreview";
import { TraceWorkersPreview } from "./preview/TraceWorkersPreview";
import "./styles.css";

const preview = import.meta.env.DEV
  ? new URLSearchParams(window.location.search).get("preview")
  : null;

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      theme={{
        algorithm: theme.darkAlgorithm,
        token: {
          colorPrimary: "#20d6ff",
          colorSuccess: "#5dff9f",
          colorWarning: "#ffc857",
          colorError: "#ff5c7a",
          colorInfo: "#7c8cff",
          colorBgBase: "#05070b",
          colorBgContainer: "rgba(12, 18, 28, 0.86)",
          colorBorder: "rgba(113, 247, 255, 0.18)",
          borderRadius: 8,
          fontFamily:
            "'IBM Plex Sans', 'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif",
          fontFamilyCode:
            "'JetBrains Mono', 'SFMono-Regular', Consolas, 'Liberation Mono', monospace"
        },
        components: {
          Button: {
            controlHeightLG: 46,
            primaryShadow: "0 0 24px rgba(32, 214, 255, 0.26)"
          },
          Input: {
            activeBorderColor: "#20d6ff",
            hoverBorderColor: "#5dff9f"
          }
        }
      }}
    >
      <AntApp>
        {preview === "run-states" ? (
          <RunStatesPreview />
        ) : preview === "conversation-layout" ? (
          <ConversationLayoutPreview />
        ) : preview === "trace-workers" ? (
          <TraceWorkersPreview />
        ) : (
          <App />
        )}
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>
);
