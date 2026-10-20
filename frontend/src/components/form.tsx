import type { ButtonHTMLAttributes, ReactNode } from "react";

export function ButtonField({
  children,
  variant = "primary",
  size,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  children: ReactNode;
  variant?: "primary" | "default" | "ghost" | "danger";
  size?: "small";
}) {
  const classes = ["button", variant === "primary" ? "primary" : "", variant === "danger" ? "danger" : "", variant === "ghost" ? "ghost" : "", size === "small" ? "small" : ""]
    .filter(Boolean)
    .join(" ");
  return (
    <button className={classes} {...props}>
      {children}
    </button>
  );
}
