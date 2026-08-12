"""
Проверка движка замены текста без Telegram — полезно, чтобы убедиться, что
всё работает на вашей машине, прежде чем запускать самого бота.

Движок сам находит векторную группу с текстом в файле (не важно, названа
она "mylogo" или вообще никак не названа — типичная ситуация для чужих
стикеров и премиум-эмодзи). Если он ошибся или в файле несколько похожих
на текст мест — можно указать пятым аргументом номер (из списка кандидатов,
который печатается при ошибке/для информации) или точное имя группы.

Использование:
    python demo_cli.py AnimatedSticker.tgs "Новый текст" output.tgs
    python demo_cli.py AnimatedSticker.tgs "Новый текст" output.tgs 2
    python demo_cli.py AnimatedSticker.tgs "Новый текст" output.tgs mylogo
"""
import sys

from tgs_editor import (
    load_tgs, save_tgs, replace_text, find_text_candidates,
    describe_candidate, MAX_TGS_BYTES, TextGroupNotFoundError,
)


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    in_path, text, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    target = sys.argv[4] if len(sys.argv) > 4 else None

    data = load_tgs(in_path)

    try:
        data, report = replace_text(data, text, target=target)
    except TextGroupNotFoundError as e:
        print(f"Не нашёл подходящую группу{f' для «{target}»' if target else ''}: {e}")
        if e.candidates:
            print("Вот что похоже на текст в этом файле:")
            for i, c in enumerate(e.candidates, start=1):
                print(" ", describe_candidate(c, i))
            print("Повторите команду с номером или именем пятым аргументом.")
        sys.exit(1)

    size = save_tgs(data, out_path)
    print(f"Сохранено: {out_path} ({size} байт из {MAX_TGS_BYTES} допустимых)")
    print(f"Нашёл текст здесь: {report['target_label']}"
          + (f" (кандидат №{report['target_index']} из {report['num_candidates']})"
             if report['num_candidates'] > 1 else ""))
    print(f"Старый размер надписи: {report['old_size']}")
    print(f"Новый размер надписи:  {report['new_size']}")
    if report['shrunk_to_fit']:
        print("  (текст длиннее оригинала — уменьшил шрифт, чтобы уместить по ширине)")
    print(f"Контуров (букв+внутренних отверстий): {report['num_new_contours']}")

    if report['num_candidates'] > 1 and not target:
        print()
        print("Другие похожие на текст места в файле (на случай, если выбрано не то):")
        for i, c in enumerate(find_text_candidates(load_tgs(in_path))[1:4], start=2):
            print(" ", describe_candidate(c, i))


if __name__ == "__main__":
    main()
