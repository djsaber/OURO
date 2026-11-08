from __future__ import annotations

_OURO_TUI_CSS = """
    /* Geometry remains fixed here; the owning app injects every visual token
       from its complete TuiTheme so CSS and Rich rendering share one source. */
    $conversation-left-gutter: 1;
    $conversation-right-gutter: 1;

    Screen {
        width: 100%;
        height: 100%;
        margin: 0;
        padding: 0;
        background: $ouro-background;
        color: $ouro-text-primary;
        layout: vertical;
    }

    /* Textual mounts notifications as screen-level system widgets. Keep that
       framework lifecycle, but project the chrome through the same frontend
       tokens as OURO's composer and menus. Severity changes only the rail
       color; geometry and surfaces remain stable. */
    ToastRack {
        dock: top;
        align: right top;
        margin-top: 1;
        margin-bottom: 0;
        scrollbar-background: transparent;
        scrollbar-color: transparent;
        scrollbar-color-hover: transparent;
        scrollbar-size-vertical: 0;
        scrollbar-gutter: auto;
    }

    Toast {
        width: auto;
        min-width: 28;
        max-width: 50%;
        height: auto;
        margin: 0 1 1 0;
        padding: 1 2;
        color: $ouro-text-primary;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-progress-track;
        border-bottom: none;
        border-left: solid $ouro-theme;
    }

    Toast.-information {
        border-left: solid $ouro-theme;
    }

    Toast.-warning {
        border-left: solid $ouro-warning;
    }

    Toast.-error {
        border-left: solid $ouro-failure;
    }

    VerticalScroll, TextArea, OptionList {
        scrollbar-background: $ouro-background;
        scrollbar-color: $ouro-border;
        scrollbar-color-hover: $ouro-scrollbar-hover;
        scrollbar-size-vertical: 1;
        scrollbar-gutter: stable;
    }

    #brand-header {
        width: 100%;
        height: 1;
        dock: top;
        padding: 0 $conversation-right-gutter 0 $conversation-left-gutter;
        background: $ouro-background;
        color: $ouro-text-primary;
    }

    #main {
        width: 100%;
        height: 1fr;
        background: $ouro-background;
    }

    #conversation {
        width: 100%;
        height: 1fr;
        padding: 1 $conversation-right-gutter 0 $conversation-left-gutter;
        background: $ouro-background;
        scrollbar-background: transparent;
        scrollbar-color: transparent;
        scrollbar-color-hover: transparent;
        scrollbar-size-vertical: 0;
        scrollbar-gutter: auto;
    }

    #conversation-transcript {
        width: 100%;
        height: auto;
        min-height: 100%;
        background: transparent;
        /* One growing history buffer and the live rows form a bounded widget
           tree. Position these siblings from their current content heights. */
        layout: vertical;
    }

    #conversation-history {
        width: 100%;
        height: auto;
        min-height: 0;
        background: transparent;
        overflow: hidden;
        scrollbar-background: transparent;
        scrollbar-color: transparent;
        scrollbar-color-hover: transparent;
        scrollbar-size: 0 0;
        scrollbar-gutter: auto;
    }

    #input-region {
        width: 100%;
        height: auto;
        margin: 0;
        padding: 0;
        color: $ouro-text-primary;
        background: $ouro-background;
    }

    #main.welcome-page #conversation {
        display: none;
    }

    #input-region.welcome-page {
        height: 100%;
    }

    #input-region.welcome-page #composer,
    #input-region.welcome-page #command-popup,
    #input-region.welcome-page #subagent-navigation-panel,
    #input-region.welcome-page StatusLine {
        width: 100%;
        margin-left: 0;
        margin-right: 0;
    }

    #input-region.welcome-page StatusLine {
        padding-left: 0;
        padding-right: 0;
    }

    .turn {
        width: 100%;
        height: auto;
        margin-bottom: 1;
        background: transparent;
    }

    .assistant-turn {
        padding: 0;
    }

    .assistant-turn.after-tool {
        padding: 1 0 0 0;
    }

    .assistant-markdown {
        width: 100%;
        height: auto;
        background: transparent;
        padding: 0;
    }

    .reasoning-view {
        height: auto;
        margin: 0 0 1 0;
        padding: 0;
        color: $ouro-text-lowlight;
        background: transparent;
    }

    .reasoning-label {
        height: 1;
        color: $ouro-text-lowlight;
        text-style: none;
    }

    /* The reasoning body carries its │ border inside its strips, matching
       the immutable history's frozen rendering cell for cell. */
    .reasoning-lines {
        width: 1fr;
        height: auto;
        margin: 1 0 0 0;
        padding: 0 1 0 0;
        color: $ouro-text-lowlight;
        background: transparent;
    }

    .reasoning-lines.collapsed {
        height: 0;
        margin-top: 0;
    }

    .tool-row {
        height: auto;
        margin: 0;
        color: $ouro-text-lowlight;
        background: transparent;
    }

    #subagent-navigation-panel {
        width: 100%;
        height: 1;
        margin: 0 $conversation-right-gutter 0 $conversation-left-gutter;
        padding: 0;
        color: $ouro-text-lowlight;
        background: $ouro-background;
        text-align: right;
        overflow: hidden;
    }

    .approval-panel {
        height: auto;
        margin: 0 $conversation-right-gutter 0 $conversation-left-gutter;
        padding: 1 2;
        color: $ouro-text-primary;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-progress-track;
        border-bottom: none;
        border-left: solid $ouro-border-subtle;
    }

    .approval-panel:focus {
        border-left: solid $ouro-theme;
        border-right: solid $ouro-border-focus;
    }

    .approval-title {
        height: 1;
    }

    .approval-controls {
        height: auto;
    }

    #interaction-bar {
        min-height: $interaction-panel-min-height;
        max-height: $interaction-panel-max-height;
        overflow-y: auto;
        scrollbar-background: transparent;
        scrollbar-color: transparent;
        scrollbar-color-hover: transparent;
        scrollbar-size-vertical: 0;
        scrollbar-gutter: auto;
    }

    #interaction-summary {
        height: auto;
        margin-bottom: 1;
    }

    #ask-user-panel {
        height: auto;
        max-height: 18;
        margin: 0 $conversation-right-gutter 0 $conversation-left-gutter;
        padding: 1 2;
        color: $ouro-text-primary;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-progress-track;
        border-bottom: none;
        border-left: solid $ouro-border-subtle;
    }

    #ask-user-panel:focus {
        border-left: solid $ouro-theme;
        border-right: solid $ouro-border-focus;
    }

    #plan-confirmation-panel {
        max-height: 24;
        overflow: hidden;
    }

    #plan-confirmation-title {
        height: 1;
        margin-bottom: 1;
    }

    #plan-confirmation-document {
        height: auto;
        max-height: 12;
        overflow-y: auto;
        scrollbar-background: transparent;
        scrollbar-color: transparent;
        scrollbar-color-hover: transparent;
        scrollbar-size-vertical: 0;
        scrollbar-gutter: auto;
    }

    #plan-confirmation-body {
        height: auto;
    }

    #plan-confirmation-controls {
        margin-top: 1;
    }

    #command-popup {
        height: auto;
        min-height: $command-popup-min-height;
        max-height: $command-popup-max-height;
        margin: 0 $conversation-right-gutter 0 $conversation-left-gutter;
        padding: 1 2;
        color: $ouro-text-primary;
        background: $ouro-panel;
        border-top: none;
        border-right: none;
        border-bottom: none;
        border-left: solid $ouro-border-subtle;
        overflow-y: hidden;
        scrollbar-gutter: auto;
    }

    #command-popup-heading {
        dock: top;
        width: 100%;
        height: 1;
    }

    #command-popup-content {
        width: 100%;
        height: auto;
    }

    /* The composer grows with its wrapped document. `height: auto` on a
       TextArea resolves through its virtual size, so Textual measures the
       text itself; the bounds below (border-box, so inclusive of the blank
       frame row above and below) cap it at three text rows and keep one row
       when empty. Past the cap the widget scrolls instead of growing. */
    #composer {
        height: auto;
        min-height: $composer-min-height;
        max-height: $composer-max-height;
        margin: 0 $conversation-right-gutter 0 $conversation-left-gutter;
        padding: 1 2;
        color: $ouro-text-emphasis;
        background: $ouro-panel;
        border-top: none;
        border-right: none;
        border-bottom: none;
        border-left: solid $ouro-border-subtle;
        scrollbar-background: transparent;
        scrollbar-color: transparent;
        scrollbar-color-hover: transparent;
        scrollbar-size-vertical: 0;
        scrollbar-gutter: auto;
    }

    #composer:focus {
        border-left: solid $ouro-theme;
    }

    #composer.read-only-session:disabled {
        opacity: 1;
    }

    #composer .text-area--suggestion {
        color: $ouro-text-disabled;
    }

    #composer .text-area--cursor-line {
        background: transparent;
    }

    #queued-input-panel {
        height: auto;
        max-height: 8;
        margin: 0 $conversation-right-gutter 0 $conversation-left-gutter;
        padding: 1 2 0 2;
        color: $ouro-text-primary;
        background: $ouro-panel;
        border-top: none;
        border-right: none;
        border-bottom: none;
        border-left: solid $ouro-border-subtle;
    }

    #queued-input-content {
        width: 100%;
        height: auto;
        background: transparent;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }

    StatusLine {
        height: 1;
        margin: 0;
        padding: 0 $conversation-right-gutter 0 $conversation-left-gutter;
        color: $ouro-text-lowlight;
        background: $ouro-background;
    }

    #activity-indicator { width: 1fr; }
    #context-detail { width: auto; text-align: right; }


"""
