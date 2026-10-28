/** 知识库来源文件的打开事件：让右侧文件面板接管预览，而不是新开浏览器页。 */
export interface OpenKbFileDetail {
   url: string;
   name: string;
   provider?: string;
   kb_name?: string;
}

export const OPEN_KB_FILE = "qi:open-kb-file";

export function openKbFile(detail: OpenKbFileDetail): void {
   window.dispatchEvent(new CustomEvent<OpenKbFileDetail>(OPEN_KB_FILE, { detail }));
}
