# CitrusScholar's fixed R adapter for the Nature marginal-scatter contract.
# Upstream does not provide an executable R plotting CLI at the pinned commit.
suppressPackageStartupMessages({
  library(ggplot2)
  library(patchwork)
})

args <- commandArgs(trailingOnly = TRUE)
stopifnot(length(args) == 1L)
run_dir <- args[[1L]]
spec <- jsonlite::read_json(file.path(run_dir, 'request.json'), simplifyVector = TRUE)
options(device = function(...) grDevices::pdf(file = NULL, ...))
source(file.path(run_dir, 'panel_alignment.R'), local = TRUE)

main <- function() {
  packages <- c(ggplot2 = '4.0.4', patchwork = '1.3.2', svglite = '2.2.2',
                ragg = '1.5.2', jsonlite = '2.0.0')
  stopifnot(getRversion() == '4.4.3', capabilities('cairo'),
            all(vapply(names(packages), function(pkg) as.character(packageVersion(pkg)), '') == packages))
  fields <- spec$plot
  raw <- read.csv('/input/observations.csv', check.names = FALSE, row.names = NULL,
                  colClasses = 'character', na.strings = NULL, fill = FALSE,
                  strip.white = FALSE, blank.lines.skip = FALSE, fileEncoding = 'UTF-8-BOM')
  stopifnot(nrow(raw) == spec$rows_input, identical(names(raw), spec$header))
  numeric_col <- function(name) {
    values <- raw[[name]]
    # Do not interpret dates, percentages or prefixes as inferred measurements.
    scientific <- '^[[:space:]]*[+-]?([0-9]+(\\.[0-9]*)?|\\.[0-9]+)([eE][+-]?[0-9]+)?[[:space:]]*$'
    parsed <- suppressWarnings(as.numeric(values))
    parsed[!grepl(scientific, values)] <- NA_real_
    parsed
  }
  groups <- if (is.null(fields$group_col)) rep('Observations', nrow(raw)) else raw[[fields$group_col]]
  values <- data.frame(x = numeric_col(fields$x_col), y = numeric_col(fields$y_col), group = groups)
  usable <- is.finite(values$x) & is.finite(values$y) & !is.na(groups) & nzchar(trimws(groups))
  if (!fields$drop_incomplete && !all(usable)) stop('incomplete CSV observations are not authorized for exclusion')
  if (!any(usable)) stop('no usable observations')
  values <- values[usable, , drop = FALSE]
  values$group <- factor(values$group, levels = unique(values$group))
  if (nlevels(values$group) > 12L) stop('marginal template supports at most 12 explicitly mapped groups')

  palette <- c('#2879A9','#B95242','#338775','#765B95','#BC782D','#4D6679',
               '#884B70','#6F7C38','#796251','#3994A6','#A35F44','#676A9A')
  style <- theme_classic(base_size = 8, base_family = 'sans') +
    theme(axis.text = element_text(size = 7, colour = '#323232'),
          axis.title = element_text(size = 8), plot.title = element_text(size = 10),
          legend.title = element_text(size = 8), legend.text = element_text(size = 7),
          plot.margin = margin(3, 3, 3, 3, unit = 'pt'))
  margin_style <- theme(axis.text = element_blank(), axis.title = element_blank(),
                        axis.ticks = element_blank(), axis.line = element_blank(),
                        legend.position = 'none')
  axis_limits <- function(x) {
    span <- range(x)
    delta <- if (span[1] == span[2]) max(abs(span[1]) * 0.05, 0.5) else diff(span) * 0.05
    result <- span + c(-delta, delta)
    if (!all(is.finite(result)) || result[1] >= result[2]) stop('finite axis extent required')
    result
  }
  xlim <- axis_limits(values$x)
  ylim <- axis_limits(values$y)
  xscale <- scale_x_continuous(limits = xlim, expand = expansion(mult = 0))
  yscale <- scale_y_continuous(limits = ylim, expand = expansion(mult = 0))
  groups_in <- !is.null(fields$group_col)
  scatter <- ggplot(values, aes(x, y, colour = group)) +
    geom_point(size = 1.35, alpha = 0.78, show.legend = groups_in) +
    scale_colour_manual(values = palette, name = fields$group_col) + xscale + yscale + style +
    labs(x = if (is.null(fields$x_label)) fields$x_col else fields$x_label,
         y = if (is.null(fields$y_label)) fields$y_col else fields$y_label)
  xhist <- ggplot(values, aes(x = x, fill = group)) +
    geom_histogram(breaks = seq(xlim[1], xlim[2], length.out = fields$bins + 1L),
                   alpha = 0.72, position = 'stack', show.legend = FALSE) +
    scale_fill_manual(values = palette) + xscale + style + margin_style
  yhist <- ggplot(values, aes(y = y, fill = group)) +
    geom_histogram(breaks = seq(ylim[1], ylim[2], length.out = fields$bins + 1L),
                   alpha = 0.72, position = 'stack', orientation = 'y', show.legend = FALSE) +
    scale_fill_manual(values = palette) + yscale + style + margin_style
  stopifnot(nrow(layer_data(scatter)) == nrow(values),
            sum(layer_data(xhist)$count) == nrow(values),
            sum(layer_data(yhist)$count) == nrow(values))
  plot <- wrap_plots(A = xhist, B = scatter, C = yhist, design = 'A#\nBC',
                     widths = c(4, 1), heights = c(1, 4), guides = 'collect') +
    plot_annotation(title = spec$title, theme = style)
  plot <- plot & theme(legend.position = 'bottom')
  width_mm <- 183
  height_mm <- 145
  width <- width_mm / 25.4
  height <- height_mm / 25.4
  out <- file.path('/outputs', spec$stem)

  # Preserve the original upstream measurement as diagnostic evidence. At the
  # pinned commit, converting null units outside panel viewports collapses axes.
  raw_layout <- withCallingHandlers(
    write_patchwork_panel_layout(plot, file.path(run_dir, 'upstream-layout.json'), width, height),
    warning = function(w) if (grepl('conversion failure', conditionMessage(w), fixed = TRUE)) invokeRestart('muffleWarning'))
  grob <- patchwork::patchworkGrob(plot)
  grDevices::cairo_pdf(paste0(out, '.pdf'), width = width, height = height, family = 'sans')
  measured <- tryCatch(draw_and_measure(grob, raw_layout), finally = grDevices::dev.off())
  jsonlite::write_json(measured, file.path(run_dir, 'layout.json'), auto_unbox = TRUE, digits = NA)
  ggsave(paste0(out, '.svg'), plot, width = width, height = height,
         device = svglite::svglite, bg = 'white')
  ggsave(paste0(out, '.tiff'), plot, width = width, height = height, dpi = 600,
         compression = 'lzw', device = ragg::agg_tiff, bg = 'white')

  qa <- list(template = 'marginal', backend = 'r', demo = FALSE,
             input = 'observations.csv', output_prefix = spec$stem,
             mapping = list(x = fields$x_col, y = fields$y_col,
                            group = if (is.null(fields$group_col)) '' else fields$group_col),
             rows_input = nrow(raw), rows_plotted = nrow(values), excluded_rows = sum(!usable),
             excluded_entities = 0L, runtime = list(r = as.character(getRversion()), packages = as.list(packages)))
  jsonlite::write_json(qa, file.path(run_dir, 'counts.json'), auto_unbox = TRUE)
}

# Capture the actual plot-area rectangles on the exact exported PDF device.
# The upstream helper supplies ordering and grid metadata, and its unchanged
# audit_panel_alignment.py decides the gate in the sealed Worker transport.
draw_and_measure <- function(grob, manifest) {
  rows <- .nature_alignment_panel_rows(grob)
  stopifnot(nrow(rows) == 3L, length(manifest$panels) == nrow(rows))
  grid::grid.newpage()
  grid::grid.draw(grob)
  grid::grid.force()
  names <- grid::grid.ls(viewports = TRUE, grobs = FALSE, print = FALSE)$name
  for (i in seq_len(nrow(rows))) {
    panel <- unique(names[startsWith(names, paste0(rows$name[i], '.'))])
    stopifnot(length(panel) == 1L)
    grid::seekViewport(panel)
    lo <- grid::deviceLoc(grid::unit(0, 'npc'), grid::unit(0, 'npc'), valueOnly = TRUE)
    hi <- grid::deviceLoc(grid::unit(1, 'npc'), grid::unit(1, 'npc'), valueOnly = TRUE)
    grid::upViewport(0)
    stopifnot(hi$x > lo$x, hi$y > lo$y)
    manifest$panels[[i]]$bbox_pt <- c(lo$x, lo$y, hi$x, hi$y) * 72
  }
  manifest
}

main()
