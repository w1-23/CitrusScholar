library(ggplot2)

print(ggplot(mtcars, aes(wt, mpg, colour = factor(cyl))) +
  geom_point() +
  scale_y_continuous(name = "MPG", sec.axis = dup_axis(name = "MPG (secondary)")) +
  labs(title = expression(alpha), x = "Vehicle mass", colour = "Cylinders"))
