#!/usr/bin/env Rscript
# Worker entry point. Run source only inside the caller's isolated R sandbox.

abort <- function(message) stop(message, call. = FALSE)
scalar_text <- function(x) is.character(x) && length(x) == 1L && !is.na(x)
json_object <- function(x) is.list(x) && !is.null(names(x)) && !anyDuplicated(names(x))

number_field <- function(request, key, default, minimum, maximum, integer = FALSE) {
  if (!key %in% names(request)) return(default)
  x <- request[[key]]
  if (!is.numeric(x) || length(x) != 1L || !is.finite(x) || x < minimum ||
      x > maximum || (integer && x != floor(x))) {
    abort(paste("Invalid request field", key))
  }
  x
}

read_request <- function(path) {
  x <- jsonlite::fromJSON(path, simplifyVector = FALSE)
  if (!json_object(x) || !scalar_text(x$format) || !x$format %in% c("svg", "png", "pdf")) {
    abort("Expected a request object with format svg, png or pdf")
  }
  overrides <- if ("overrides" %in% names(x)) x$overrides else setNames(list(), character())
  if (!json_object(overrides)) abort("Expected overrides to be a JSON object")
  for (id in names(overrides)) {
    entry <- overrides[[id]]
    if (!grepl("^[A-Za-z0-9._:-]{1,160}$", id) || !json_object(entry) ||
        !identical(names(entry), "text") || !scalar_text(entry$text)) {
      abort("Expected every override to have a valid object ID and a single text string")
    }
  }
  width <- number_field(x, "width_mm", 178, 1, 1000)
  height <- number_field(x, "height_mm", 110, 1, 1000)
  dpi <- number_field(x, "dpi", 300, 1, 1200, integer = TRUE)
  if (x$format == "png" && (width * height * (dpi / 25.4)^2 > 4e7 ||
      min(width, height) * dpi / 25.4 < 1)) abort("PNG dimensions must be between 1 pixel per side and 40 million total pixels")
  list(format = x$format, width = width / 25.4, height = height / 25.4, dpi = dpi,
       overrides = overrides,
       ordinal = number_field(x, "figure_ordinal", NULL, 0, 7, integer = TRUE))
}

open_device <- function(folder, request) {
  file <- file.path(folder, paste0("page-%04d.", request$format))
  switch(request$format,
    svg = svglite::svglite(file, width = request$width, height = request$height),
    png = ragg::agg_png(file, width = request$width, height = request$height,
                        units = "in", res = request$dpi),
    pdf = grDevices::cairo_pdf(file, width = request$width, height = request$height, onefile = FALSE)
  )
  as.integer(grDevices::dev.cur())
}

page_files <- function(folder) list.files(folder, "^page-[0-9]+\\.(svg|png|pdf)$", full.names = TRUE)

source_figure <- function(path, folder, request) {
  before <- grDevices::dev.list()
  device <- open_device(folder, request)
  on.exit({
    added <- setdiff(grDevices::dev.list(), before)
    for (i in added) grDevices::dev.off(i)
  }, add = TRUE)
  grDevices::dev.control("enable")
  scope <- new.env(parent = globalenv())
  # Keep unseeded source behaviour repeatable across independent worker runs.
  set.seed(1L)
  result <- source(path, local = scope, chdir = TRUE, encoding = "UTF-8")
  if (!setequal(grDevices::dev.list(), c(before, device)) || grDevices::dev.cur() != device) {
    abort("Source must draw on the worker's graphics device")
  }
  drawn <- length(grDevices::recordPlot()[[1L]]) > 0L
  grDevices::dev.off(device)
  pages <- page_files(folder)
  named <- get0("figure", envir = scope, inherits = FALSE)
  plot <- if (inherits(named, "ggplot")) named else result$value
  if (is.null(request$ordinal)) {
    if (length(pages) > 1L) abort("Only single-page figures are supported in legacy import")
    return(list(plot = plot, drawn = drawn, pages = pages))
  }
  # Explicit R figure bundles may return a named `figures` list. Drawn pages
  # without an explicit native mapping are deliberately static candidates.
  declared <- get0("figures", envir = scope, inherits = FALSE)
  index <- as.integer(request$ordinal) + 1L
  if (is.list(declared) && length(declared) && all(vapply(declared, inherits, logical(1), "ggplot"))) {
    count <- length(declared)
    if (count > 8L || index > count) abort("Figure ordinal not captured")
    return(list(plot = declared[[index]], count = count, drawn = FALSE, pages = character()))
  }
  if (length(pages) > 1L && drawn) {
    count <- length(pages)
    if (count > 8L || index > count) abort("Figure ordinal not captured")
    return(list(plot = NULL, count = count, drawn = TRUE, pages = pages[index]))
  }
  if (index != 1L) abort("Figure ordinal not captured")
  list(plot = plot, count = 1L, drawn = drawn, pages = pages)
}

# Use the layout built by ggplot2 itself, including facets and its resolved guides.
layout_grobs <- function(table, pattern) {
  if (!inherits(table, "gtable")) return(list())
  table$grobs[grep(pattern, table$layout$name)]
}

visible_grobs <- function(grobs) {
  Filter(function(grob) !inherits(grob, "zeroGrob"), grobs)
}

single_text_grob <- function(grob) {
  if (inherits(grob, "text")) return(grob$label)
  children <- grob$children
  if (length(children) == 1L && inherits(children[[1L]], "text")) return(children[[1L]]$label)
  NULL
}

is_cartesian <- function(plot) {
  inherits(plot$coordinates, "CoordCartesian") && !inherits(plot$coordinates, "CoordFlip")
}

panel_records <- function(built) {
  layout <- built$layout$layout
  variables <- setdiff(names(layout), c("PANEL", "ROW", "COL", "SCALE_X", "SCALE_Y",
                                         "COORD", "AXIS_X", "AXIS_Y"))
  lapply(seq_len(nrow(layout)), function(i) {
    id <- paste0("panel.", layout$PANEL[[i]])
    label <- if (length(variables)) {
      paste(vapply(variables, function(key) paste0(key, " = ", as.character(layout[[key]][[i]])),
                   character(1)), collapse = ", ")
    } else paste("Panel", layout$PANEL[[i]])
    list(id = id, label = label)
  })
}

object_record <- function(id, panel, kind, label, text) {
  list(id = id, panel_id = panel, kind = kind, label = label,
       properties = list(text = text), editable = list("text"))
}

collect_legends <- function(guides, table) {
  boxes <- layout_grobs(table, "^guide-box($|-)")
  containers <- unlist(lapply(boxes, layout_grobs, pattern = "^guides$"), recursive = FALSE)
  grobs <- visible_grobs(unlist(lapply(containers, layout_grobs, pattern = "^title$"),
                               recursive = FALSE))
  titles <- Filter(scalar_text, lapply(grobs, single_text_grob))
  params <- guides$params
  found <- list()
  for (i in seq_along(params)) {
    p <- params[[i]]
    aes <- sort(unique(p$aesthetic))
    if (!scalar_text(p$title) || !length(aes) ||
        !all(grepl("^[A-Za-z0-9_]+$", aes)) ||
        !inherits(guides$guides[[i]], c("GuideLegend", "GuideColourbar", "GuideColoursteps"))) next
    # Do not guess which of repeated titles belongs to which guide.
    if (sum(vapply(titles, identical, logical(1), p$title)) != 1L ||
        sum(vapply(params, function(entry) identical(entry$title, p$title), logical(1))) != 1L) next
    id <- paste0("legend.", paste(aes, collapse = "."), ".title")
    if (nchar(id) > 160L || !is.null(found[[id]])) next
    found[[id]] <- list(aes = aes, guide = guides$guides[[i]], text = p$title)
  }
  list(entries = found[sort(names(found))], hidden = length(grobs) - length(found))
}

collect_figure <- function(plot) {
  built <- ggplot2::ggplot_build(plot)
  table <- ggplot2::ggplot_gtable(built)
  panels <- panel_records(built)
  panel <- panels[[1L]]$id
  objects <- list()
  slots <- c("plot.title" = "^title$", "axis.x.title" = "^xlab-[bt]$", "axis.y.title" = "^ylab-[lr]$")
  labels <- c("Plot title", "X axis title", "Y axis title")
  unsupported <- FALSE
  for (i in seq_along(slots)) {
    grobs <- visible_grobs(layout_grobs(table, slots[[i]]))
    if (!length(grobs)) next
    text <- if (length(grobs) == 1L) single_text_grob(grobs[[1L]]) else NULL
    axis <- i != 1L
    if (!scalar_text(text) || (axis && !is_cartesian(plot))) {
      unsupported <- TRUE
      next
    }
    id <- names(slots)[[i]]
    objects[[id]] <- object_record(id, panel, if (axis) "axis_title" else "title", labels[[i]], text)
  }
  legends <- collect_legends(built$plot$guides, table)
  for (id in names(legends$entries)) {
    entry <- legends$entries[[id]]
    label <- paste0("Legend title (", paste(entry$aes, collapse = ", "), ")")
    objects[[id]] <- object_record(id, panel, "legend_title", label, entry$text)
  }
  list(table = table, panels = panels, objects = objects, legends = legends$entries,
       partial = unsupported || legends$hidden > 0L)
}

guide_with_title <- function(parent, text) {
  force(parent)
  params <- parent$params
  params$title <- text
  ggplot2::ggproto(NULL, parent, params = params)
}

apply_axis_title <- function(plot, axis, text) {
  plot$scales <- plot$scales$clone()
  scale <- plot$scales$get_scales(axis)
  guide <- plot$guides$get_guide(axis)
  if (is.null(guide) && !is.null(scale) && inherits(scale$guide, "Guide")) guide <- scale$guide
  if (inherits(guide, "GuideAxis") && !ggplot2::is_waiver(guide$params$title)) {
    guide <- guide_with_title(guide, text)
    plot <- plot + do.call(ggplot2::guides, setNames(list(guide), axis))
  } else if (!is.null(scale) && !ggplot2::is_waiver(scale$name)) {
    scale$name <- text
  }
  plot + do.call(ggplot2::labs, setNames(list(text), axis))
}

apply_overrides <- function(plot, entries, overrides) {
  if (!all(names(overrides) %in% names(entries$objects))) {
    abort("One or more overrides refer to unavailable text objects")
  }
  for (id in names(overrides)) {
    text <- overrides[[id]]$text
    if (id == "plot.title") {
      plot <- plot + ggplot2::labs(title = text)
    } else if (id %in% c("axis.x.title", "axis.y.title")) {
      plot <- apply_axis_title(plot, substr(id, 6L, 6L), text)
    } else {
      entry <- entries$legends[[id]]
      guide <- guide_with_title(entry$guide, text)
      plot <- plot + do.call(ggplot2::guides, setNames(rep(list(guide), length(entry$aes)), entry$aes))
    }
  }
  plot
}

verify_overrides <- function(original, updated, overrides) {
  for (id in names(original$objects)) {
    expected <- if (id %in% names(overrides)) overrides[[id]]$text else original$objects[[id]]$properties$text
    if (!identical(updated$objects[[id]]$properties$text, expected)) {
      abort(paste("Native edit did not preserve the expected text for object", id))
    }
  }
}

render_ggplot <- function(plot, folder, request) {
  device <- open_device(folder, request)
  on.exit(grDevices::dev.off(device), add = TRUE)
  seed <- get(".Random.seed", envir = globalenv())
  entries <- collect_figure(plot)
  if (length(request$overrides)) {
    plot <- apply_overrides(plot, entries, request$overrides)
    assign(".Random.seed", seed, envir = globalenv())
    updated <- collect_figure(plot)
    verify_overrides(entries, updated, request$overrides)
    entries <- updated
  }
  # gtable construction may already have initialized the grid device page.
  grid::grid.draw(entries$table)
  limitations <- c("Only existing plain-text plot, primary axis and supported legend titles are editable. Data, geometry, tick labels, legend entries, facets and styling are not editable.")
  if (length(entries$panels) > 1L) limitations <- c(limitations, "Plot-level objects are associated with the first facet for indexing only.")
  if (entries$partial) limitations <- c(limitations, "Some rendered titles could not be mapped unambiguously to native ggplot2 text fields.")
  list(editability = if (!length(entries$objects)) "layout_only" else if (entries$partial) "partial" else "semantic",
       panels = entries$panels, objects = unname(entries$objects), limitations = as.list(limitations))
}

capture_signature <- function(witness) {
  line <- system2("/usr/local/bin/python3", c("-I", "-c",
    shQuote("import hashlib,sys; print(hashlib.file_digest(open(sys.argv[1], 'rb'), 'sha256').hexdigest())"),
    shQuote(witness)), stdout = TRUE)
  if (length(line) != 1L || !grepl("^[a-f0-9]{64}$", line)) abort("No capture signature")
  line
}

worker_main <- function(args) {
  if (length(args) != 3L) abort("Usage: r_render.R source.R request.json output_dir")
  source_path <- normalizePath(args[[1L]], mustWork = TRUE)
  out <- normalizePath(args[[3L]], mustWork = TRUE)
  if (!dir.exists(out)) abort("Output directory does not exist")
  request <- read_request(args[[2L]])
  staging <- tempfile(pattern = ".r-render-", tmpdir = out)
  if (!dir.create(staging, mode = "0700")) abort("Could not create a staging directory")
  on.exit(unlink(staging, recursive = TRUE), add = TRUE)
  raw <- file.path(staging, "raw")
  native <- file.path(staging, "native")
  dir.create(raw)
  dir.create(native)
  source_request <- request
  if (!is.null(request$ordinal)) {
    source_request$format <- "png"
    source_request$width <- 178 / 25.4
    source_request$height <- 110 / 25.4
    source_request$dpi <- 120
  }
  captured <- source_figure(source_path, raw, source_request)
  plot <- captured$plot
  editable <- inherits(plot, "ggplot") && !inherits(plot, c("patchwork", "ggmatrix", "ggarrange"))
  signature <- NULL
  if (!is.null(request$ordinal) && editable) {
    # Witness the unmodified object before configuring or drawing on the export device.
    signature_folder <- file.path(staging, "signature")
    dir.create(signature_folder)
    signature_request <- list(format = "png", width = 7, height = 4.5, dpi = 80, overrides = list())
    withr::with_seed(57L, render_ggplot(plot, signature_folder, signature_request))
    witness <- page_files(signature_folder)
    if (length(witness) != 1L) abort("No capture witness")
    signature <- capture_signature(witness)
  }
  if (editable) {
    renderer <- list(id = "r-ggplot2", version = as.character(utils::packageVersion("ggplot2")))
    meta <- render_ggplot(plot, native, request)
    pages <- page_files(native)
  } else {
    if (length(request$overrides)) abort("This figure has no editable native text objects")
    if (!captured$drawn) abort("No supported figure was returned or drawn by the script")
    pages <- captured$pages
    renderer <- list(id = if (inherits(plot, c("ggplot", "patchwork", "ggmatrix", "ggarrange"))) "r-static" else "r-base",
                     version = as.character(getRversion()))
    meta <- list(editability = "layout_only", panels = list(list(id = "panel.1", label = "Figure")),
                 objects = list(), limitations = list("This source drew a static graphics page. Native text, data and geometry are not editable."))
  }
  if (length(pages) != 1L || !isTRUE(file.size(pages) > 0)) abort("Expected one nonempty rendered page")
  result <- c(list(renderer = renderer), meta)
  if (!is.null(request$ordinal)) {
    if (is.null(signature)) signature <- capture_signature(pages)
    result$capture <- list(ordinal = as.integer(request$ordinal), count = captured$count, signature = signature)
  }
  result_path <- file.path(staging, "result.json")
  jsonlite::write_json(result, result_path, auto_unbox = TRUE, pretty = TRUE)
  if (!file.rename(pages, file.path(out, paste0("figure.", request$format))) ||
      !file.rename(result_path, file.path(out, "result.json"))) abort("Could not publish worker output")
}

if (sys.nframe() == 0L) {
  tryCatch(worker_main(commandArgs(trailingOnly = TRUE)), error = function(e) {
    message("r_render: ", conditionMessage(e))
    quit(save = "no", status = 1L, runLast = FALSE)
  })
}
