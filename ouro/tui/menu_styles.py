"""Shared menu chrome for the main TUI and its offline recovery surface."""

MENU_CSS = """
    ModalScreen {
        align: center middle;
        overflow: hidden;
        /* Exactly one visible menu owns the focus backdrop. Parent menu panels
           become fully transparent while a child page is active, preventing
           nested darkening without losing the menu's visual focus. */
        background: $ouro-background 65%;
    }

    /* All modal menus share one flat chrome with the composer and command
       menu: a low-contrast panel with a muted edge on the right. The left
       rail is segmented — the title carries the theme accent, the body
       carries the lowlight — so the header reads as emphasis and the body
       as structure, matching the "quiet frame + bright header" contrast of
       the input box. */
    #choice-panel {
        width: 1fr;
        max-width: 72;
        margin: 0 2;
        height: auto;
        min-height: 12;
        max-height: 27;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-progress-track;
        border-bottom: none;
        border-left: none;
    }

    #choice-title {
        width: 100%;
        height: 2;
        padding: 1 2 0 2;
        color: $ouro-theme;
        text-style: bold;
        background: transparent;
        border: none;
        border-left: solid $ouro-theme;
    }

    #choice-description {
        width: 100%;
        height: auto;
        max-height: 3;
        padding: 0 2;
        color: $ouro-text-lowlight;
        background: transparent;
        border-left: solid $ouro-theme;
    }

    #choice-filter {
        width: 100%;
        height: 3;
        padding: 1 2;
        background: transparent;
        border: none;
        border-left: solid $ouro-theme;
    }

    #choice-list {
        width: 100%;
        height: auto;
        min-height: 5;
        max-height: 20;
        padding: 0 1 1 1;
    }

    /* Shared grouped-menu treatment. Choice and inline-edit screens use the
       same rows, group headers, focus color, and hidden chrome. */
    .grouped-menu-list {
        background: transparent;
        border: none;
        border-left: solid $ouro-separator;
        /* The menu is keyboard-first: arrow navigation keeps the highlighted
           option in view, so the scrollbar is pure chrome and is hidden to
           match the flat panels used elsewhere. */
        scrollbar-background: transparent;
        scrollbar-color: transparent;
        scrollbar-color-hover: transparent;
        scrollbar-size-vertical: 0;
        scrollbar-gutter: auto;
    }

    .grouped-menu-list .option-list--option-highlighted {
        color: $ouro-theme;
        background: $ouro-highlight;
        text-style: not bold;
    }

    .grouped-menu-list:focus .option-list--option-highlighted {
        color: $ouro-theme;
        background: $ouro-input-highlight;
        text-style: not bold;
    }

    #choice-hint {
        height: 2;
        padding: 0 2 1 2;
        color: $ouro-text-lowlight;
        background: transparent;
        border: none;
        border-left: solid $ouro-separator;
    }

"""
