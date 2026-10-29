"use client";
// FE-DEC-02, FE-DEC-04, FE-DS-02 (R5): port of `QuestionBlock` in bundle.js. Numbered heading (h3), optional help, an answer
// input (OptionCards choice or yes/no, or a compact Dropzone for upload) and an optional comment box behind "Add comment".
// Added over bundle.js (P12 needs them to build a DecisionAnswer): `onOtherChange`, `defaultOther`, `onCommentChange`; the
// comment box takes the focus only when the user opens it, not when it starts open.
import { useId, useState, type ReactNode } from "react";
import { Button } from "./Button";
import { Dropzone } from "./Dropzone";
import { FileRow } from "./FileRow";
import { OptionCards, type OptionCardsProps } from "./OptionCards";
import { TextField } from "./TextField";
import { nonEmpty, truthy } from "./utils";
import { COPY } from "@/lib/copy";

export interface QuestionBlockProps {
  number?: string;
  question: string;
  help?: string;
  type?: "choice" | "yesno" | "upload";
  options?: NonNullable<OptionCardsProps["options"]>;
  allowOther?: boolean;
  defaultValue?: string;
  onChange?: (v: string) => void;
  onOtherChange?: (text: string) => void;
  defaultOther?: string;
  onFiles?: (f: File[]) => void;
  uploadTitle?: string;
  defaultCommentOpen?: boolean;
  onCommentChange?: (text: string) => void;
  /** Initial comment text (a recorded comment, or a draft). */
  defaultComment?: string;
  /** Recorded answer view (FE-DEC-03.AC8): the answer and the comment cannot be changed, no "Add comment" button. */
  readOnly?: boolean;
  /** Upload: the chosen file name, shown in the drop zone (or as a file row when read-only) (FE-DEC-03.AC5). */
  fileName?: string;
}

type QuestionInputProps = Pick<
  QuestionBlockProps,
  | "type"
  | "question"
  | "options"
  | "allowOther"
  | "defaultValue"
  | "onChange"
  | "onOtherChange"
  | "defaultOther"
  | "onFiles"
  | "uploadTitle"
  | "readOnly"
  | "fileName"
>;

/** The answer control of a question: the attached file or the drop zone for an upload, else the option cards. */
function QuestionInput({
  type,
  question,
  options,
  allowOther,
  defaultValue,
  onChange,
  onOtherChange,
  defaultOther,
  onFiles,
  uploadTitle,
  readOnly,
  fileName,
}: Readonly<QuestionInputProps>) {
  if (type === "upload") {
    if (truthy(readOnly) && truthy(fileName)) return <FileRow name={fileName} status="done" doneText="Attached" />;
    return (
      <Dropzone
        compact
        title={nonEmpty(fileName) ?? nonEmpty(uploadTitle) ?? "Attach a supporting document"}
        hint={truthy(fileName) ? "Chosen file. Drop another one to replace it." : "Demo only: files are not uploaded"}
        onFiles={onFiles}
      />
    );
  }
  return (
    <OptionCards
      type={type === "yesno" ? "yesno" : "choice"}
      options={options}
      allowOther={allowOther}
      label={question}
      defaultValue={defaultValue}
      onChange={onChange}
      onOtherChange={onOtherChange}
      defaultOther={defaultOther}
      disabled={readOnly}
    />
  );
}

export function QuestionBlock({
  number,
  question,
  help,
  type,
  options,
  allowOther,
  defaultValue,
  onChange,
  onOtherChange,
  defaultOther,
  onFiles,
  uploadTitle,
  defaultCommentOpen,
  onCommentChange,
  defaultComment,
  readOnly,
  fileName,
}: Readonly<QuestionBlockProps>) {
  const [showComment, setShowComment] = useState(defaultCommentOpen === true || truthy(defaultComment));
  const [userOpened, setUserOpened] = useState(false);
  const questionId = useId();
  let comment: ReactNode = null;
  if (showComment) {
    comment = (
      <TextField
        label="Comment (optional)"
        multiline
        rows={3}
        placeholder={COPY.reviewerComment}
        autoFocus={userOpened}
        defaultValue={defaultComment}
        readOnly={readOnly}
        onChange={(e) => onCommentChange?.(e.target.value)}
      />
    );
  } else if (!truthy(readOnly)) {
    comment = (
      <Button
        variant="plain"
        size="sm"
        aria-describedby={questionId}
        onClick={() => {
          setUserOpened(true);
          setShowComment(true);
        }}
      >
        Add comment
      </Button>
    );
  }
  return (
    <section className="gdg-q">
      <div className="gdg-q__head">
        <span className="gdg-q__num">{nonEmpty(number) ?? "Q"}</span>
        <div>
          <h3 className="gdg-q__title" id={questionId}>
            {question}
          </h3>
          {truthy(help) ? <p className="gdg-q__help">{help}</p> : null}
        </div>
      </div>
      <QuestionInput
        type={type}
        question={question}
        options={options}
        allowOther={allowOther}
        defaultValue={defaultValue}
        onChange={onChange}
        onOtherChange={onOtherChange}
        defaultOther={defaultOther}
        onFiles={onFiles}
        uploadTitle={uploadTitle}
        readOnly={readOnly}
        fileName={fileName}
      />
      {comment}
    </section>
  );
}
