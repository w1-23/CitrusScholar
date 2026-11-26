# Synthetic neutral experimental readouts. No user data or installed service.
suppressPackageStartupMessages(library(ggplot2))
stopifnot(R.version$major == '4', R.version$minor == '4.3',
          as.character(packageVersion('ggplot2')) == '4.0.4',
          as.character(packageVersion('svglite')) == '2.2.2')
rows <- data.frame(x = rep(1:4, 2), y = c(1, 2, 2.3, 3.4, 1.4, 2.2, 3.1, 2.8),
                   group = rep(c('Control', 'Replicate'), each = 4))
chart <- ggplot(rows, aes(x, y, color = group)) + geom_line(linewidth = .7) +
  geom_point(size = 2) + labs(title = 'Pinned ggplot2 response', x = 'Time', y = 'Signal') + theme_classic()
svglite::svglite(commandArgs(trailingOnly = TRUE)[1], width = 5, height = 3)
print(chart)
invisible(dev.off())
