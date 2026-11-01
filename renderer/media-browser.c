/* SPDX-License-Identifier: GPL-3.0-or-later
 * Included in pqiv.c after action_done(). All callbacks execute on GTK's main
 * thread. stdout is a private, inherited event pipe; stdin carries pqiv actions.
 * No shells, subprocesses, global XInput observers, or geometry polling.
 */
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

static GdkRGBA mb_background, mb_foreground, mb_accent, mb_accent_text;
static GPtrArray *mb_view_nodes = NULL;
static guint64 mb_search_revision = 0, mb_view_revision = 0;
static guint mb_search_marked = 0, mb_search_hidden = 0;

static void mb_diagnostic(const gchar *name, const gchar *text) {
	const gchar *directory = g_getenv("MEDIA_BROWSER_SESSION");
	if(!directory || !g_getenv("MEDIA_BROWSER_TRACE")) return;
	gchar *path = g_build_filename(directory, name, NULL);
	g_file_set_contents(path, text, -1, NULL);
	g_free(path);
}

static void mb_canvas_background(cairo_t *cr, gboolean transparent) {
	cairo_set_source_rgba(cr, mb_background.red, mb_background.green, mb_background.blue,
		transparent ? 0. : 1.);
}

static void mb_window_background(GdkWindow *window) {
	if(!window) return;
	GdkRGBA color = mb_background;
	if(option_transparent_background) color.alpha = 0.;
	gdk_window_set_background_rgba(window, &color);
}

static void mb_theme_changed(GtkWidget *widget, gpointer unused) {
	GtkStyleContext *style = gtk_widget_get_style_context(widget);
	gtk_style_context_get_background_color(style, GTK_STATE_FLAG_NORMAL, &mb_background);
	gtk_style_context_get_color(style, GTK_STATE_FLAG_NORMAL, &mb_foreground);
	if(!gtk_style_context_lookup_color(style, "theme_base_color", &mb_background))
		gtk_style_context_lookup_color(style, "theme_bg_color", &mb_background);
	gtk_style_context_lookup_color(style, "theme_text_color", &mb_foreground);
	mb_accent = mb_foreground;
	mb_accent_text = mb_background;
	gtk_style_context_lookup_color(style, "theme_selected_bg_color", &mb_accent);
	gtk_style_context_lookup_color(style, "theme_selected_fg_color", &mb_accent_text);
	option_box_colors.bg_red = mb_accent.red;
	option_box_colors.bg_green = mb_accent.green;
	option_box_colors.bg_blue = mb_accent.blue;
	option_box_colors.fg_red = mb_accent_text.red;
	option_box_colors.fg_green = mb_accent_text.green;
	option_box_colors.fg_blue = mb_accent_text.blue;
	GdkWindow *window = gtk_widget_get_window(widget);
	mb_window_background(window);
	gchar *background = gdk_rgba_to_string(&mb_background);
	gchar *foreground = gdk_rgba_to_string(&mb_foreground);
	gchar *accent = gdk_rgba_to_string(&mb_accent);
	gchar *theme = NULL;
	gboolean dark = FALSE;
	g_object_get(gtk_settings_get_default(), "gtk-theme-name", &theme,
		"gtk-application-prefer-dark-theme", &dark, NULL);
	gchar *state = g_strdup_printf("[theme]\nname=%s\nbackground=%s\nforeground=%s\naccent=%s\nprefer_dark=%s\n",
		theme, background, foreground, accent, dark ? "true" : "false");
	mb_diagnostic("theme-state.ini", state);
	g_free(state); g_free(theme); g_free(background); g_free(foreground); g_free(accent);
	/* Redraw UI colors only. Existing tile surfaces and cached masters stay intact. */
	gtk_widget_queue_draw(widget);
}

static const gchar *mb_action_description(key_binding_t *binding) {
	int amount = binding->parameter.pint;
	switch(binding->action) {
		case ACTION_MB_HELP: return "Show shortcut help";
		case ACTION_MB_SEARCH_FOCUS: return "Focus fuzzy media search";
		case ACTION_MB_COPY_REQUEST: return amount ? "Copy original full path" : "Copy original image";
		case ACTION_MB_ROW: return amount ? "Last thumbnail in this row" : "First thumbnail in this row";
		case ACTION_MB_EVENT:
			if(!strcmp(binding->parameter.pcharptr, "visual")) return "Start/stop visual range";
			if(!strcmp(binding->parameter.pcharptr, "stop")) return "Stop range; preserve marks";
			if(!strcmp(binding->parameter.pcharptr, "clear")) return "Clear every mark and range";
			if(!strcmp(binding->parameter.pcharptr, "open")) return "Open current original (unmarked only)";
			if(!strcmp(binding->parameter.pcharptr, "trash")) return "Confirm moving originals to Trash";
			if(!strcmp(binding->parameter.pcharptr, "sizeup")) return "Larger thumbnails (up to 384)";
			if(!strcmp(binding->parameter.pcharptr, "sizedown")) return "Smaller thumbnails (down to 64)";
			return "Application event";
		case ACTION_MONTAGE_MODE_SHIFT_X: return amount < 0 ? "Move left" : "Move right";
		case ACTION_MONTAGE_MODE_SHIFT_Y: return amount < 0 ? "Move up" : "Move down";
		case ACTION_MONTAGE_MODE_SHIFT_Y_PG: return amount < 0 ? "Previous page" : "Next page";
		case ACTION_MONTAGE_MODE_FOLLOW: return "Quick-jump labels (asdfghjkl)";
		case ACTION_GOTO_FILE_BYINDEX: return amount == 0 ? "First item" : amount == -1 ? "Last item" : "Go to numbered item";
		case ACTION_GOTO_FILE_RELATIVE: return amount < 0 ? "Previous photo" : "Next photo";
		case ACTION_SET_SCALE_LEVEL_RELATIVE: return binding->parameter.pdouble > 1. ? "Zoom in" : "Zoom out";
		case ACTION_SHIFT_X: return amount > 0 ? "Pan left" : "Pan right";
		case ACTION_SHIFT_Y: return amount > 0 ? "Pan up" : "Pan down";
		case ACTION_RESET_SCALE_LEVEL: return "Reset image zoom";
		case ACTION_TOGGLE_FULLSCREEN: return "Toggle fullscreen";
		case ACTION_QUIT: return "Close this window";
		default: return pqiv_action_descriptors[binding->action].name;
	}
}

struct mb_help_data { GtkListStore *store; GString *text; const gchar *prefix; };
static const gchar *mb_help_section(key_binding_t *binding) {
	if(binding->action == ACTION_MB_COPY_REQUEST) return "Clipboard";
	if(binding->action == ACTION_MB_EVENT) {
		const gchar *event = binding->parameter.pcharptr;
		return g_str_has_prefix(event, "size") ? "Zoom & size" : !strcmp(event, "open") ? "Navigation" : "Selection & Trash";
	}
	if(binding->action == ACTION_QUIT || binding->action == ACTION_MB_HELP || binding->action == ACTION_TOGGLE_FULLSCREEN) return "Help & window";
	if(binding->action == ACTION_SHIFT_X || binding->action == ACTION_SHIFT_Y || binding->action == ACTION_SET_SCALE_LEVEL_RELATIVE || binding->action == ACTION_RESET_SCALE_LEVEL) return "Zoom & pan";
	if(binding->action == ACTION_MB_ROW || (binding->action >= ACTION_MONTAGE_MODE_SHIFT_X && binding->action <= ACTION_MONTAGE_MODE_RETURN_CANCEL) || binding->action == ACTION_GOTO_FILE_BYINDEX || binding->action == ACTION_GOTO_FILE_RELATIVE) return "Navigation";
	return "Other pqiv controls";
}

static gint mb_help_sort(GtkTreeModel *model, GtkTreeIter *a, GtkTreeIter *b, gpointer unused) {
	gchar *ak, *ag, *bk, *bg;
	gtk_tree_model_get(model, a, 0, &ak, 2, &ag, -1);
	gtk_tree_model_get(model, b, 0, &bk, 2, &bg, -1);
	gint order = g_strcmp0(ag, bg);
	if(!order) order = g_strcmp0(ak, bk);
	g_free(ak); g_free(ag); g_free(bk); g_free(bg);
	return order;
}

static void mb_help_binding(gpointer key, gpointer value, gpointer user_data) {
	struct mb_help_data *data = user_data;
	key_binding_t *binding = value;
	gchar *sequence = key_binding_sequence_to_string(GPOINTER_TO_UINT(key), (gchar *)data->prefix);
	if(binding->next_key_bindings) {
		struct mb_help_data nested = { data->store, data->text, sequence };
		g_hash_table_foreach(binding->next_key_bindings, mb_help_binding, &nested);
	}
	GString *description = g_string_new(NULL);
	for(key_binding_t *action = binding; action; action = action->next_action) {
		if(action->action == ACTION_NOP) continue;
		if(action->action == ACTION_NUMERIC_COMMAND && (action->parameter.pint < 1 || action->parameter.pint > 9 || !external_image_filter_commands[action->parameter.pint - 1])) continue;
		if(description->len) g_string_append(description, "; ");
		g_string_append(description, mb_action_description(action));
	}
	if(description->len) {
		GtkTreeIter row;
		gtk_list_store_append(data->store, &row);
		gtk_list_store_set(data->store, &row, 0, sequence, 1, description->str, 2, mb_help_section(binding), -1);
		g_string_append_printf(data->text, "%s\t%s\n", sequence, description->str);
	}
	g_string_free(description, TRUE);
	g_free(sequence);
}

struct mb_help_ui {
	GtkWidget *dialog, *entry, *tabs, *status, *views[2], *scrolls[2];
	GtkTreeModelFilter *filters[2];
};

static gboolean mb_fuzzy_word(const gchar *query, const gchar *text) {
	for(const gchar *q = query; *q; q = g_utf8_next_char(q)) {
		gunichar wanted = g_utf8_get_char(q);
		if(wanted == '+') continue; /* Ctrl+u also matches pqiv's <Control>u. */
		while(*text && g_utf8_get_char(text) != wanted) text = g_utf8_next_char(text);
		if(!*text) return FALSE;
		text = g_utf8_next_char(text);
	}
	return TRUE;
}

static gboolean mb_help_visible(GtkTreeModel *model, GtkTreeIter *row, gpointer entry) {
	gchar *query = g_utf8_casefold(gtk_entry_get_text(GTK_ENTRY(entry)), -1);
	gchar *keys, *action, *section;
	gtk_tree_model_get(model, row, 0, &keys, 1, &action, 2, &section, -1);
	gchar *joined = g_strjoin(" ", keys, action, section,
		strstr(keys, "<question>") ? "?" : "", strstr(keys, "<dollar>") ? "$" : "", NULL);
	gchar *text = g_utf8_casefold(joined, -1);
	gchar **words = g_strsplit_set(query, " \t\r\n", -1);
	gboolean matches = TRUE;
	for(gchar **word = words; *word && matches; word++) matches = mb_fuzzy_word(*word, text);
	g_strfreev(words); g_free(query); g_free(keys); g_free(action); g_free(section); g_free(joined); g_free(text);
	return matches;
}

static void mb_help_snapshot(struct mb_help_ui *ui) {
	int page = MAX(0, gtk_notebook_get_current_page(GTK_NOTEBOOK(ui->tabs)));
	GtkAdjustment *scroll = gtk_scrolled_window_get_vadjustment(GTK_SCROLLED_WINDOW(ui->scrolls[page]));
	GKeyFile *state = g_key_file_new();
	g_key_file_set_string(state, "help", "query", gtk_entry_get_text(GTK_ENTRY(ui->entry)));
	g_key_file_set_string(state, "help", "focus", gtk_window_get_focus(GTK_WINDOW(ui->dialog)) == ui->entry ? "search" : "list");
	g_key_file_set_double(state, "help", "scroll", gtk_adjustment_get_value(scroll));
	g_key_file_set_double(state, "help", "upper", gtk_adjustment_get_upper(scroll));
	g_key_file_set_double(state, "help", "page_size", gtk_adjustment_get_page_size(scroll));
	GString *visible = g_string_new(NULL);
	for(int index = 0; index < 2; index++) {
		GtkTreeModel *model = GTK_TREE_MODEL(ui->filters[index]);
		g_key_file_set_integer(state, "help", index == 0 ? "gallery_count" : "photo_count", gtk_tree_model_iter_n_children(model, NULL));
		g_string_append_printf(visible, "[%s]\n", index == 0 ? "Gallery" : "Photo");
		GtkTreeIter row;
		for(gboolean valid = gtk_tree_model_get_iter_first(model, &row); valid; valid = gtk_tree_model_iter_next(model, &row)) {
			gchar *keys, *action;
			gtk_tree_model_get(model, &row, 0, &keys, 1, &action, -1);
			g_string_append_printf(visible, "%s\t%s\n", keys, action);
			g_free(keys); g_free(action);
		}
	}
	gchar *text = g_key_file_to_data(state, NULL, NULL);
	mb_diagnostic("help-filter-state.ini", text);
	mb_diagnostic("help-filter.tsv", visible->str);
	g_free(text); g_string_free(visible, TRUE); g_key_file_unref(state);
}

static void mb_help_changed(GtkEditable *entry, gpointer dialog) {
	struct mb_help_ui *ui = g_object_get_data(G_OBJECT(dialog), "mb-help-ui");
	for(int page = 0; page < 2; page++) gtk_tree_model_filter_refilter(ui->filters[page]);
	int gallery = gtk_tree_model_iter_n_children(GTK_TREE_MODEL(ui->filters[0]), NULL);
	int photo = gtk_tree_model_iter_n_children(GTK_TREE_MODEL(ui->filters[1]), NULL);
	gchar *message = gallery + photo ? g_strdup_printf("Gallery: %d matches · Photo: %d matches", gallery, photo) : g_strdup("No matching shortcuts");
	gtk_label_set_text(GTK_LABEL(ui->status), message);
	g_free(message);
	mb_help_snapshot(ui);
}

static void mb_help_stop_search(GtkSearchEntry *entry, gpointer dialog) {
	gtk_dialog_response(GTK_DIALOG(dialog), GTK_RESPONSE_CLOSE);
}

static gboolean mb_help_key(GtkWidget *dialog, GdkEventKey *event, gpointer user_data) {
	struct mb_help_ui *ui = user_data;
	GtkWidget *focus = gtk_window_get_focus(GTK_WINDOW(dialog));
	if(event->keyval == GDK_KEY_Escape) {
		gtk_dialog_response(GTK_DIALOG(dialog), GTK_RESPONSE_CLOSE);
		return TRUE;
	}
	int page = MAX(0, gtk_notebook_get_current_page(GTK_NOTEBOOK(ui->tabs)));
	if(focus == ui->entry || (focus && GTK_IS_EDITABLE(focus))) {
		if(event->keyval == GDK_KEY_Tab && !(event->state & GDK_SHIFT_MASK)) {
			gtk_widget_grab_focus(ui->views[page]);
			mb_help_snapshot(ui);
			return TRUE;
		}
		return FALSE; /* j/k/q/? are ordinary query text in the input box. */
	}
	if(event->keyval == GDK_KEY_question || event->keyval == GDK_KEY_q) {
		gtk_dialog_response(GTK_DIALOG(dialog), GTK_RESPONSE_CLOSE);
		return TRUE;
	}
	if(event->keyval == GDK_KEY_slash) {
		gtk_widget_grab_focus(ui->entry);
		mb_help_snapshot(ui);
		return TRUE;
	}
	if((event->keyval == GDK_KEY_j || event->keyval == GDK_KEY_k) && !(event->state & (GDK_CONTROL_MASK | GDK_MOD1_MASK | GDK_SUPER_MASK))) {
		GtkAdjustment *scroll = gtk_scrolled_window_get_vadjustment(GTK_SCROLLED_WINDOW(ui->scrolls[page]));
		double step = MAX(28., gtk_adjustment_get_step_increment(scroll));
		double value = gtk_adjustment_get_value(scroll) + (event->keyval == GDK_KEY_j ? step : -step);
		double lower = gtk_adjustment_get_lower(scroll);
		double upper = MAX(lower, gtk_adjustment_get_upper(scroll) - gtk_adjustment_get_page_size(scroll));
		gtk_adjustment_set_value(scroll, CLAMP(value, lower, upper));
		mb_help_snapshot(ui);
		return TRUE;
	}
	return FALSE;
}

static void mb_help(void) {
	GtkWidget *dialog = gtk_dialog_new_with_buttons("Media Browser — Shortcuts", main_window,
		GTK_DIALOG_MODAL | GTK_DIALOG_DESTROY_WITH_PARENT, "_Close", GTK_RESPONSE_CLOSE, NULL);
	gtk_window_set_default_size(GTK_WINDOW(dialog), MIN(840, MAX(320, main_window_width - 40)),
		MIN(650, MAX(300, main_window_height - 40)));
	GtkWidget *content = gtk_dialog_get_content_area(GTK_DIALOG(dialog));
	struct mb_help_ui *ui = g_new0(struct mb_help_ui, 1);
	ui->dialog = dialog;
	g_object_set_data_full(G_OBJECT(dialog), "mb-help-ui", ui, g_free);
	GtkWidget *intro = gtk_label_new("Fuzzy search keys, actions or sections, including your overrides.\nTab: list · j/k: scroll · /: search · Escape: close (q/? also close outside the input).\nCopy uses the original; cursor/marks are preserved. Video playback follows mpv's configuration.");
	gtk_label_set_line_wrap(GTK_LABEL(intro), TRUE);
	gtk_box_pack_start(GTK_BOX(content), intro, FALSE, FALSE, 10);
	GtkWidget *tabs = gtk_notebook_new();
	ui->tabs = tabs;
	ui->entry = gtk_search_entry_new();
	gtk_entry_set_placeholder_text(GTK_ENTRY(ui->entry), "Search shortcuts — e.g. cpimg, copy path, ctrl+u");
	gtk_entry_set_max_length(GTK_ENTRY(ui->entry), 256);
	gtk_box_pack_start(GTK_BOX(content), ui->entry, FALSE, FALSE, 4);
	ui->status = gtk_label_new(NULL);
	gtk_label_set_xalign(GTK_LABEL(ui->status), 0.);
	gtk_box_pack_start(GTK_BOX(content), ui->status, FALSE, FALSE, 2);
	GString *text = g_string_new(NULL);
	for(int page = 0; page < 2; page++) {
		enum context_t context = page == 0 ? MONTAGE : DEFAULT;
		const gchar *title = page == 0 ? "Gallery" : "Photo";
		GtkListStore *store = gtk_list_store_new(3, G_TYPE_STRING, G_TYPE_STRING, G_TYPE_STRING);
		g_string_append_printf(text, "[%s]\n", title);
		struct mb_help_data data = { store, text, "" };
		g_hash_table_foreach(key_bindings[context], mb_help_binding, &data);
		gtk_tree_sortable_set_sort_func(GTK_TREE_SORTABLE(store), 2, mb_help_sort, NULL, NULL);
		gtk_tree_sortable_set_sort_column_id(GTK_TREE_SORTABLE(store), 2, GTK_SORT_ASCENDING);
		GtkTreeModel *filtered = gtk_tree_model_filter_new(GTK_TREE_MODEL(store), NULL);
		ui->filters[page] = GTK_TREE_MODEL_FILTER(filtered);
		gtk_tree_model_filter_set_visible_func(ui->filters[page], mb_help_visible, g_object_ref(ui->entry), g_object_unref);
		GtkWidget *view = gtk_tree_view_new_with_model(filtered);
		ui->views[page] = view;
		gtk_tree_view_set_enable_search(GTK_TREE_VIEW(view), FALSE);
		gtk_tree_view_insert_column_with_attributes(GTK_TREE_VIEW(view), -1, "Section", gtk_cell_renderer_text_new(), "text", 2, NULL);
		gtk_tree_view_insert_column_with_attributes(GTK_TREE_VIEW(view), -1, "Keys", gtk_cell_renderer_text_new(), "text", 0, NULL);
		GtkCellRenderer *description = gtk_cell_renderer_text_new();
		g_object_set(description, "wrap-mode", PANGO_WRAP_WORD_CHAR, "wrap-width", 300, NULL);
		gtk_tree_view_insert_column_with_attributes(GTK_TREE_VIEW(view), -1, "Action", description, "text", 1, NULL);
		g_object_unref(store);
		g_object_unref(filtered);
		GtkWidget *scroll = gtk_scrolled_window_new(NULL, NULL);
		ui->scrolls[page] = scroll;
		gtk_container_add(GTK_CONTAINER(scroll), view);
		gtk_notebook_append_page(GTK_NOTEBOOK(tabs), scroll, gtk_label_new(title));
	}
	mb_diagnostic("help-state.tsv", text->str);
	g_string_free(text, TRUE);
	gtk_box_pack_start(GTK_BOX(content), tabs, TRUE, TRUE, 6);
	gtk_widget_show_all(content);
	gtk_notebook_set_current_page(GTK_NOTEBOOK(tabs), application_mode == MONTAGE ? 0 : 1);
	g_signal_connect_object(ui->entry, "changed", G_CALLBACK(mb_help_changed), dialog, 0);
	g_signal_connect_object(ui->entry, "stop-search", G_CALLBACK(mb_help_stop_search), dialog, 0);
	g_signal_connect(dialog, "key-press-event", G_CALLBACK(mb_help_key), ui);
	gtk_window_set_focus(GTK_WINDOW(dialog), ui->entry);
	mb_help_changed(GTK_EDITABLE(ui->entry), dialog);
	g_object_ref(dialog);
	gtk_dialog_run(GTK_DIALOG(dialog));
	gtk_widget_destroy(dialog);
	g_object_unref(dialog);
}

static void mb_clipboard_error(const gchar *message) {
	mb_diagnostic("clipboard-error.txt", message);
	GtkWidget *dialog = gtk_message_dialog_new(main_window, GTK_DIALOG_DESTROY_WITH_PARENT,
		GTK_MESSAGE_ERROR, GTK_BUTTONS_CLOSE, "%s", message);
	gtk_window_set_title(GTK_WINDOW(dialog), "Media Browser — Copy failed");
	g_signal_connect_swapped(dialog, "response", G_CALLBACK(gtk_widget_destroy), dialog);
	gtk_widget_show(dialog);
}

struct mb_copy_job { gchar *path; struct stat fingerprint; guint64 generation; };
static struct mb_copy_job *mb_pending_copy = NULL;
static GCancellable *mb_copy_cancellable = NULL;
static gboolean mb_copy_busy = FALSE;
static guint64 mb_copy_generation = 0;
static void mb_copy_start(void);

static void mb_copy_free(struct mb_copy_job *job) { if(job) { g_free(job->path); g_free(job); } }
static void mb_copy_done(struct mb_copy_job *job, GError *error) {
	if(error && !g_error_matches(error, G_IO_ERROR, G_IO_ERROR_CANCELLED) && job->generation == mb_copy_generation)
		mb_clipboard_error(error->message);
	g_clear_error(&error);
	mb_copy_free(job);
	g_clear_object(&mb_copy_cancellable);
	mb_copy_busy = FALSE;
	mb_copy_start();
}

static void mb_clipboard_record(const gchar *path, const gchar *mode, int width, int height) {
	GKeyFile *state = g_key_file_new();
	g_key_file_set_string(state, "clipboard", "path", path);
	g_key_file_set_string(state, "clipboard", "mode", mode);
	g_key_file_set_integer(state, "clipboard", "width", width);
	g_key_file_set_integer(state, "clipboard", "height", height);
	g_key_file_set_uint64(state, "clipboard", "generation", mb_copy_generation);
	gchar *text = g_key_file_to_data(state, NULL, NULL);
	mb_diagnostic("clipboard-state.ini", text);
	g_free(text); g_key_file_unref(state);
}

static void mb_copy_decoded(GObject *source, GAsyncResult *result, gpointer user_data) {
	struct mb_copy_job *job = user_data;
	GError *error = NULL;
	GdkPixbuf *image = gdk_pixbuf_new_from_stream_finish(result, &error);
	struct stat now;
	if(image && job->generation == mb_copy_generation) {
		struct stat *old = &job->fingerprint;
		if(stat(job->path, &now) != 0 || old->st_dev != now.st_dev || old->st_ino != now.st_ino ||
			old->st_size != now.st_size || old->st_mtim.tv_sec != now.st_mtim.tv_sec ||
			old->st_mtim.tv_nsec != now.st_mtim.tv_nsec || old->st_ctim.tv_sec != now.st_ctim.tv_sec || old->st_ctim.tv_nsec != now.st_ctim.tv_nsec) {
			g_set_error_literal(&error, G_IO_ERROR, G_IO_ERROR_FAILED, "The original changed while copying. Please copy it again.");
		}
		else {
			GdkPixbuf *oriented = gdk_pixbuf_apply_embedded_orientation(image);
			if(oriented) {
				GtkClipboard *clipboard = gtk_clipboard_get(GDK_SELECTION_CLIPBOARD);
				gtk_clipboard_set_image(clipboard, oriented);
				gtk_clipboard_set_can_store(clipboard, NULL, 0);
				gtk_clipboard_store(clipboard);
				mb_clipboard_record(job->path, "image", gdk_pixbuf_get_width(oriented), gdk_pixbuf_get_height(oriented));
				g_object_unref(oriented);
			}
		}
	}
	g_clear_object(&image);
	mb_copy_done(job, error);
}

static void mb_copy_opened(GObject *source, GAsyncResult *result, gpointer user_data) {
	struct mb_copy_job *job = user_data;
	GError *error = NULL;
	GFileInputStream *stream = g_file_read_finish(G_FILE(source), result, &error);
	if(!stream) { mb_copy_done(job, error); return; }
	gdk_pixbuf_new_from_stream_async(G_INPUT_STREAM(stream), mb_copy_cancellable, mb_copy_decoded, job);
	g_object_unref(stream);
}

static void mb_copy_start(void) {
	if(mb_copy_busy || !mb_pending_copy) return;
	struct mb_copy_job *job = mb_pending_copy;
	mb_pending_copy = NULL;
	mb_copy_busy = TRUE;
	mb_copy_cancellable = g_cancellable_new();
	GFile *file = g_file_new_for_path(job->path);
	g_file_read_async(file, G_PRIORITY_DEFAULT, mb_copy_cancellable, mb_copy_opened, job);
	g_object_unref(file);
}

static void mb_copy(const gchar *parameter) {
	const gchar *encoded = strchr(parameter, ':');
	if(!encoded) return;
	gsize length;
	guchar *decoded = g_base64_decode(encoded + 1, &length);
	gchar *path = g_strndup((gchar *)decoded, length);
	g_free(decoded);
	mb_copy_generation++;
	if(mb_copy_cancellable) g_cancellable_cancel(mb_copy_cancellable);
	mb_copy_free(mb_pending_copy); mb_pending_copy = NULL;
	if(g_str_has_prefix(parameter, "motion:")) {
		mb_clipboard_error("Image copying is for still photos. Open animations/videos in mpv, or use Y to copy their original path.");
		g_free(path);
		return;
	}
	if(g_str_has_prefix(parameter, "path:")) {
		gchar *text = g_filename_to_utf8(path, -1, NULL, NULL, NULL);
		if(text) {
			GtkClipboard *clipboard = gtk_clipboard_get(GDK_SELECTION_CLIPBOARD);
			gtk_clipboard_set_text(clipboard, text, -1);
			gtk_clipboard_set_can_store(clipboard, NULL, 0);
			gtk_clipboard_store(clipboard);
			mb_clipboard_record(path, "path", 0, 0);
			g_free(text);
		}
		g_free(path);
		return;
	}
	struct mb_copy_job *job = g_new0(struct mb_copy_job, 1);
	job->path = path; job->generation = mb_copy_generation;
	if(stat(path, &job->fingerprint) != 0) {
		mb_clipboard_error("The original image is no longer available."); mb_copy_free(job); return;
	}
	mb_pending_copy = job;
	mb_copy_start();
}

static void mb_copy_request(gboolean path) {
	if(application_mode == MONTAGE && g_getenv("MEDIA_BROWSER_EVENTS")) {
		mb_emit(path ? "copy_path" : "copy_image");
		return;
	}
	D_LOCK(file_tree);
	if(current_file_node && g_getenv("MEDIA_BROWSER_PHOTO_VIEWER")) {
		gchar *request = g_strconcat(path ? "P" : "I", CURRENT_FILE->file_name, NULL);
		/* stdout is an inherited connected datagram endpoint for photo requests. */
		if(send(STDOUT_FILENO, request, strlen(request), MSG_NOSIGNAL) < 0)
			mb_clipboard_error("The gallery is no longer available for copying.");
		g_free(request);
	}
	D_UNLOCK(file_tree);
}

static gint64 mb_catalog_id(BOSNode *node) {
	if(!node) return -1;
	const gchar *base = strrchr(FILE(node)->file_name, G_DIR_SEPARATOR);
	base = base ? base + 1 : FILE(node)->file_name;
	gchar *end = NULL;
	gint64 id = g_ascii_strtoll(base, &end, 10);
	return end != base && strcmp(end, ".png") == 0 ? id : -1;
}

static BOSNode *mb_find(gint64 id) {
	for(BOSNode *n = bostree_select(file_tree, 0); n; n = bostree_next_node(n)) {
		if(mb_catalog_id(n) == id) return n;
	}
	return NULL;
}

static void mb_emit(const gchar *event) {
	const gchar *photo_state = g_getenv("MEDIA_BROWSER_PHOTO_STATE");
	if(photo_state && application_mode == DEFAULT) {
		D_LOCK(file_tree);
		if(current_file_node && CURRENT_FILE->is_loaded) {
			gchar *name = g_base64_encode((const guchar *)CURRENT_FILE->file_name, strlen(CURRENT_FILE->file_name));
			gchar scale[G_ASCII_DTOSTR_BUF_SIZE];
			g_ascii_dtostr(scale, sizeof(scale), current_scale_level);
			gchar *state = g_strdup_printf("{\"path_base64\":\"%s\",\"scale\":%s,\"shift_x\":%d,\"shift_y\":%d,\"width\":%d,\"height\":%d,\"scale_mode\":%d,\"scale_override\":%s}",
				name, scale, current_shift_x, current_shift_y, main_window_width, main_window_height,
				option_scale, scale_override ? "true" : "false");
			g_file_set_contents(photo_state, state, -1, NULL);
			g_free(state); g_free(name);
		}
		D_UNLOCK(file_tree);
	}
	if(!g_getenv("MEDIA_BROWSER_EVENTS") || application_mode != MONTAGE || !mb_gallery_entry) return;
	D_LOCK(file_tree);
	BOSNode *selected = montage_window_control.selected_node;
	BOSNode *node = selected ? bostree_node_weak_unref(file_tree, bostree_node_weak_ref(selected)) : NULL;
	gint columns = MAX(1, main_window_width / (option_thumbnails.width + 10));
	gint rows = MAX(1, main_window_height / (option_thumbnails.height + 10));
	gchar *query = mb_gallery_entry ? g_base64_encode((const guchar *)gtk_entry_get_text(GTK_ENTRY(mb_gallery_entry)), strlen(gtk_entry_get_text(GTK_ENTRY(mb_gallery_entry)))) : g_strdup("");
	int origin_x = 0, origin_y = 0;
	if(mb_gallery_canvas) gtk_widget_translate_coordinates(mb_gallery_canvas, GTK_WIDGET(main_window), 0, 0, &origin_x, &origin_y);
	gchar *line = g_strdup_printf("{\"event\":\"%s\",\"id\":%" G_GINT64_FORMAT ",\"first\":%d,\"count\":%d,\"columns\":%d,\"rows\":%d,\"size\":%d,\"total\":%u,\"library_total\":%lu,\"revision\":%" G_GUINT64_FORMAT ",\"view_revision\":%" G_GUINT64_FORMAT ",\"query\":\"%s\",\"origin_x\":%d,\"origin_y\":%d,\"viewport_width\":%d,\"viewport_height\":%d}",
		event, mb_catalog_id(node), montage_window_control.scroll_y * columns, columns * rows,
		columns, rows, option_thumbnails.width, mb_view_count(), (unsigned long)bostree_node_count(file_tree), mb_search_revision, mb_view_revision, query, origin_x * screen_scale_factor, origin_y * screen_scale_factor, main_window_width, main_window_height);
	g_free(query);
	D_UNLOCK(file_tree);
	static gchar *previous = NULL;
	if(strcmp(event, "state") != 0 || !previous || strcmp(previous, line) != 0) {
		puts(line);
		fflush(stdout);
	}
	g_free(previous);
	previous = line;
}

static gboolean mb_state_idle(gpointer unused) {
	mb_emit("state");
	return G_SOURCE_REMOVE;
}

static gboolean mb_dialog_key(GtkWidget *dialog, GdkEventKey *event, gpointer unused) {
	guint key = gdk_keyval_to_lower(event->keyval);
	if(key == GDK_KEY_y) gtk_dialog_response(GTK_DIALOG(dialog), GTK_RESPONSE_YES);
	else if(key == GDK_KEY_n || key == GDK_KEY_Escape) gtk_dialog_response(GTK_DIALOG(dialog), GTK_RESPONSE_NO);
	else return FALSE;
	return TRUE;
}

static void mb_dialog(const gchar *path, gboolean error) {
	gchar *contents = NULL;
	gsize length = 0;
	if(!g_file_get_contents(path, &contents, &length, NULL)) { mb_emit("no"); return; }
	GString *listing = g_string_new(NULL);
	guint count = 0;
	for(gsize i = 0; i < length;) {
		gsize n = strnlen(contents + i, length - i);
		if(n == length - i) { g_free(contents); g_string_free(listing, TRUE); mb_emit("no"); return; }
		count++;
		if(count <= 5 || error) g_string_append_printf(listing, "%s\n", contents + i);
		i += n + 1;
	}
	if(count == 0) { g_free(contents); g_string_free(listing, TRUE); mb_emit("no"); return; }
	gchar *question = error ? g_strdup("Could not move the file to Trash.") :
		(count == 1 ? g_strdup("Move this file to Trash?") : g_strdup_printf("Move %u files to Trash?", count));
	GtkWidget *dialog = gtk_message_dialog_new(main_window, GTK_DIALOG_MODAL | GTK_DIALOG_DESTROY_WITH_PARENT,
		error ? GTK_MESSAGE_ERROR : GTK_MESSAGE_QUESTION, GTK_BUTTONS_NONE, "%s", question);
	gtk_window_set_title(GTK_WINDOW(dialog), error ? "Media browser — Trash failed" : "Media browser — Move to Trash?");
	gtk_message_dialog_format_secondary_text(GTK_MESSAGE_DIALOG(dialog), "%s%s",
		(error || count <= 5) ? listing->str : "", error ? "" : "\nPress y to confirm or n to cancel.");
	/* Optional diagnostics read back from the actual GTK widget properties. */
	if(g_getenv("MEDIA_BROWSER_TRACE")) {
		gchar *primary = NULL, *secondary = NULL;
		g_object_get(dialog, "text", &primary, "secondary-text", &secondary, NULL);
		gchar *shown = g_strconcat(primary ? primary : "", "\n", secondary ? secondary : "", NULL);
		gchar *shown_path = g_strconcat(path, ".shown", NULL);
		g_file_set_contents(shown_path, shown, -1, NULL);
		g_free(primary); g_free(secondary); g_free(shown); g_free(shown_path);
	}
	if(error) gtk_dialog_add_button(GTK_DIALOG(dialog), "_Close", GTK_RESPONSE_NO);
	else gtk_dialog_add_buttons(GTK_DIALOG(dialog), "_No", GTK_RESPONSE_NO, "_Yes", GTK_RESPONSE_YES, NULL);
	gtk_dialog_set_default_response(GTK_DIALOG(dialog), GTK_RESPONSE_NO);
	g_signal_connect(dialog, "key-press-event", G_CALLBACK(mb_dialog_key), NULL);
	g_object_ref(dialog);
	gint response = gtk_dialog_run(GTK_DIALOG(dialog));
	gtk_widget_destroy(dialog);
	g_object_unref(dialog);
	if(!error) mb_emit(response == GTK_RESPONSE_YES ? "yes" : "no");
	g_free(question);
	g_free(contents);
	g_string_free(listing, TRUE);
}

static void mb_mark(const gchar *value) {
	gchar *end = NULL;
	gint64 id = g_ascii_strtoll(value, &end, 10);
	if(end == value || *end != ':') return;
	D_LOCK(file_tree);
	BOSNode *node = mb_find(id);
	if(node) FILE(node)->marked = end[1] == '1';
	D_UNLOCK(file_tree);
	queue_draw();
}

static guint mb_view_count(void) {
	return mb_view_nodes ? mb_view_nodes->len : file_tree ? bostree_node_count(file_tree) : 0;
}
static BOSNode *mb_view_select(guint index) {
	return mb_view_nodes ? (index < mb_view_nodes->len ? g_ptr_array_index(mb_view_nodes, index) : NULL) : bostree_select(file_tree, index);
}
static guint mb_view_rank(BOSNode *node) {
	if(!mb_view_nodes) return bostree_rank(node);
	for(guint index = 0; index < mb_view_nodes->len; index++) if(g_ptr_array_index(mb_view_nodes, index) == node) return index;
	return G_MAXUINT;
}
static BOSNode *mb_view_next(BOSNode *node) {
	if(!mb_view_nodes) return bostree_next_node(node);
	guint index = mb_view_rank(node);
	return mb_view_select(index == G_MAXUINT ? 0 : index + 1);
}
static BOSNode *mb_view_previous(BOSNode *node) {
	if(!mb_view_nodes) return bostree_previous_node(node);
	guint index = mb_view_rank(node);
	return index == 0 ? NULL : mb_view_select(index == G_MAXUINT ? mb_view_count() - 1 : index - 1);
}
static void mb_view_clear(void) {
	if(!mb_view_nodes) return;
	for(guint index = 0; index < mb_view_nodes->len; index++) bostree_node_weak_unref(file_tree, g_ptr_array_index(mb_view_nodes, index));
	g_ptr_array_free(mb_view_nodes, TRUE);
	mb_view_nodes = NULL;
}
static void mb_view_prune(void) {
	if(!mb_view_nodes) return;
	for(guint index = 0; index < mb_view_nodes->len;) {
		BOSNode *node = g_ptr_array_index(mb_view_nodes, index);
		if(!bostree_node_weak_unref(file_tree, bostree_node_weak_ref(node))) {
			g_ptr_array_remove_index(mb_view_nodes, index);
			bostree_node_weak_unref(file_tree, node);
		} else index++;
	}
}

static void mb_search_status(const gchar *parameter) {
	if(!mb_gallery_status) return;
	if(parameter) {
		gchar **fields = g_strsplit(parameter, ";", 2);
		mb_search_marked = (guint)g_ascii_strtoull(fields[0], NULL, 10);
		mb_search_hidden = fields[1] ? (guint)g_ascii_strtoull(fields[1], NULL, 10) : 0;
		g_strfreev(fields);
	}
	gchar *hidden = mb_search_hidden ? g_strdup_printf(" (%u hidden)", mb_search_hidden) : g_strdup("");
	gchar *text = mb_search_revision != mb_view_revision ? g_strdup("Searching…") :
		g_strdup_printf("%u / %u · %u marked%s", mb_view_count(), bostree_node_count(file_tree), mb_search_marked, hidden);
	gtk_label_set_text(GTK_LABEL(mb_gallery_status), text);
	g_free(text); g_free(hidden);
}

static void mb_set_view(const gchar *parameter) {
	if(!mb_gallery_entry) return;
	gsize length;
	guchar *decoded = g_base64_decode(parameter, &length);
	gchar *text = g_strndup((gchar *)decoded, length);
	g_free(decoded);
	gchar **fields = g_strsplit(text, ";", 3);
	if(!fields[0] || !fields[1] || !fields[2]) { g_strfreev(fields); g_free(text); return; }
	guint64 revision = g_ascii_strtoull(fields[0], NULL, 10);
	if(revision != mb_search_revision) { g_strfreev(fields); g_free(text); return; }
	gint64 selected_id = g_ascii_strtoll(fields[1], NULL, 10);
	gchar **ids = g_strsplit(fields[2], ",", -1);
	GHashTable *wanted = g_hash_table_new_full(g_int64_hash, g_int64_equal, g_free, NULL);
	for(gchar **id = ids; *id; id++) {
		if(!**id) continue;
		gint64 *value = g_new(gint64, 1); *value = g_ascii_strtoll(*id, NULL, 10);
		g_hash_table_add(wanted, value);
	}
	D_LOCK(file_tree);
	mb_view_clear();
	if(*gtk_entry_get_text(GTK_ENTRY(mb_gallery_entry))) {
		mb_view_nodes = g_ptr_array_new();
		for(BOSNode *node = bostree_select(file_tree, 0); node; node = bostree_next_node(node)) {
			gint64 id = mb_catalog_id(node);
			if(g_hash_table_contains(wanted, &id)) g_ptr_array_add(mb_view_nodes, bostree_node_weak_ref(node));
		}
	}
	BOSNode *selected = selected_id >= 0 ? mb_find(selected_id) : NULL;
	if(montage_window_control.selected_node) bostree_node_weak_unref(file_tree, montage_window_control.selected_node);
	montage_window_control.selected_node = selected ? bostree_node_weak_ref(selected) : NULL;
	montage_window_control.scroll_y = 0;
	mb_view_revision = revision;
	if(mb_view_count()) montage_window_move_cursor(0, 0, 0);
	else abort_pending_image_loads(NULL);
	mb_search_status(NULL);
	D_UNLOCK(file_tree);
	g_hash_table_unref(wanted); g_strfreev(ids); g_strfreev(fields); g_free(text);
	queue_draw(); mb_emit("view");
}

static void mb_gallery_query(GtkEditable *entry, gpointer unused) {
	mb_search_revision++;
	mb_search_status(NULL);
	mb_emit("search");
}
static void mb_gallery_browse(GtkEntry *entry, gpointer unused) { gtk_widget_grab_focus(mb_gallery_canvas); }
static gboolean mb_gallery_draw(GtkWidget *widget, cairo_t *cr, gpointer data) {
	window_draw_callback(widget, cr, data);
	return TRUE;
}
static void mb_gallery_allocate(GtkWidget *widget, GtkAllocation *allocation, gpointer unused) {
	main_window_width = MAX(1, allocation->width * screen_scale_factor);
	main_window_height = MAX(1, allocation->height * screen_scale_factor);
	if(file_tree && application_mode == MONTAGE) {
		D_LOCK(file_tree); montage_window_move_cursor(0, 0, 0); D_UNLOCK(file_tree);
		g_idle_add(mb_state_idle, NULL);
	}
}
static void mb_gallery_setup(void) {
	gtk_orientable_set_orientation(GTK_ORIENTABLE(main_window_box), GTK_ORIENTATION_VERTICAL);
	GtkWidget *bar = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 8);
	gtk_container_set_border_width(GTK_CONTAINER(bar), 6);
	mb_gallery_entry = gtk_search_entry_new();
	g_signal_connect(mb_gallery_entry, "destroy", G_CALLBACK(gtk_widget_destroyed), &mb_gallery_entry);
	gtk_entry_set_placeholder_text(GTK_ENTRY(mb_gallery_entry), "Search filenames or folders (fuzzy) · / to focus");
	gtk_entry_set_max_length(GTK_ENTRY(mb_gallery_entry), 256);
	gtk_box_pack_start(GTK_BOX(bar), mb_gallery_entry, TRUE, TRUE, 0);
	mb_gallery_status = gtk_label_new("Loading…");
	g_signal_connect(mb_gallery_status, "destroy", G_CALLBACK(gtk_widget_destroyed), &mb_gallery_status);
	gtk_box_pack_start(GTK_BOX(bar), mb_gallery_status, FALSE, FALSE, 0);
	gtk_box_pack_start(GTK_BOX(main_window_box), bar, FALSE, FALSE, 0);
	mb_gallery_canvas = gtk_drawing_area_new();
	g_signal_connect(mb_gallery_canvas, "destroy", G_CALLBACK(gtk_widget_destroyed), &mb_gallery_canvas);
	gtk_widget_set_can_focus(mb_gallery_canvas, TRUE);
	gtk_widget_add_events(mb_gallery_canvas, GDK_BUTTON_PRESS_MASK | GDK_BUTTON_RELEASE_MASK | GDK_SCROLL_MASK | GDK_POINTER_MOTION_MASK | GDK_KEY_PRESS_MASK);
	gtk_box_pack_start(GTK_BOX(main_window_box), mb_gallery_canvas, TRUE, TRUE, 0);
	g_signal_connect(mb_gallery_canvas, "draw", G_CALLBACK(mb_gallery_draw), NULL);
	g_signal_connect(mb_gallery_canvas, "size-allocate", G_CALLBACK(mb_gallery_allocate), NULL);
	g_signal_connect(mb_gallery_canvas, "button-press-event", G_CALLBACK(window_button_press_callback), NULL);
	g_signal_connect(mb_gallery_canvas, "button-release-event", G_CALLBACK(window_button_release_callback), NULL);
	g_signal_connect(mb_gallery_canvas, "motion-notify-event", G_CALLBACK(window_motion_notify_callback), NULL);
	g_signal_connect(mb_gallery_canvas, "scroll-event", G_CALLBACK(window_scroll_callback), NULL);
	g_signal_connect(mb_gallery_entry, "changed", G_CALLBACK(mb_gallery_query), NULL);
	g_signal_connect(mb_gallery_entry, "activate", G_CALLBACK(mb_gallery_browse), NULL);
	g_signal_connect(mb_gallery_entry, "stop-search", G_CALLBACK(mb_gallery_browse), NULL);
	gtk_widget_show_all(main_window_box);
	gtk_window_set_focus(main_window, mb_gallery_canvas);
}
