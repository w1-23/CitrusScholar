library(ggplot2)

figure <- ggplot(mtcars, aes(wt, mpg, colour = factor(cyl), fill = factor(cyl))) +
  geom_point(shape = 21, size = 3) +
  scale_x_continuous(name = "Mass (1000 lb)", guide = guide_axis(title = "Vehicle mass")) +
  scale_y_continuous(name = "Miles per gallon") +
  guides(colour = guide_legend(title = "Cylinders"), fill = guide_legend(title = "Cylinders")) +
  labs(title = "Fuel economy — original") +
  facet_wrap(vars(am)) +
  theme_minimal()
