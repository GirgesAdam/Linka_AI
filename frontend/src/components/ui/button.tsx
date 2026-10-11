import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex shrink-0 cursor-pointer items-center justify-center gap-2 rounded-xl text-sm font-bold transition-[background-color,border-color,color,box-shadow,transform] duration-150 motion-reduce:transition-none disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:ring-offset-2 active:translate-y-px motion-reduce:active:translate-y-0",
  {
    variants: {
      variant: {
        default: "bg-[var(--accent)] text-white shadow-[0_1px_2px_rgba(15,23,42,.08)] hover:-translate-y-px hover:bg-[var(--accent-strong)] hover:shadow-[0_5px_12px_rgba(101,70,215,.18)] active:translate-y-0 active:shadow-[0_1px_2px_rgba(15,23,42,.08)] motion-reduce:hover:translate-y-0",
        secondary: "border border-transparent bg-[var(--surface-2)] text-slate-800 hover:bg-[var(--surface-3)]",
        outline: "border border-[var(--border)] bg-white text-slate-700 shadow-[0_1px_1px_rgba(15,23,42,.02)] hover:border-[var(--border-strong)] hover:bg-slate-50 hover:text-slate-950",
        ghost: "text-slate-600 hover:bg-[var(--surface-2)] hover:text-slate-950",
        danger: "bg-red-600 text-white shadow-[0_1px_2px_rgba(127,29,29,.12)] hover:bg-red-700 hover:shadow-[0_3px_8px_rgba(127,29,29,.16)] active:shadow-none",
      },
      size: {
        default: "h-10 px-4",
        sm: "h-8 px-3 text-xs",
        lg: "h-11 px-5",
        icon: "size-10 p-0",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {}

export function Button({ className, variant, size, ...props }: ButtonProps) {
  return <button className={cn(buttonVariants({ variant, size }), className)} {...props} />;
}

export { buttonVariants };
