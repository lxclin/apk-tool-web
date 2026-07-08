from gui import APKToolApp
import tkinter as tk


def create_root():
    return tk.Tk()


def main():
    root = create_root()
    APKToolApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
