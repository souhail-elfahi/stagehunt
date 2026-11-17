/**
 * Internship Hunter — date de candidature automatique (Google Apps Script, déclencheur simple).
 *
 * Quand tu mets un Statut "postulé ou plus loin" dans l'onglet Offres et que la cellule
 * "Date candidature" de la même ligne est vide, la date du jour y est écrite (dans le fuseau
 * horaire du classeur, réglé par setup_sheet.py depuis config.yaml).
 *
 * - Les colonnes sont trouvées par le nom de leur en-tête (ligne 1), jamais par leur position :
 *   déplacer des colonnes ne casse rien. Si un des deux en-têtes manque ou est en double,
 *   le script ne fait rien.
 * - Une date déjà présente n'est jamais modifiée ni effacée (même si le Statut revient en arrière).
 * - Gère les modifications de plusieurs lignes à la fois (collage, recopie vers le bas).
 * - Ne réagit qu'à tes modifications dans le navigateur : les écritures du robot (API) ne
 *   déclenchent pas onEdit.
 * - Déclencheur simple : aucune autorisation demandée, il n'accède qu'à ce classeur.
 */

var SHEET_NAME = 'Offres';
var STATUS_HEADER = 'Statut';
var DATE_HEADER = 'Date candidature';
// Même liste que APPLIED dans sheet_lib.py.
var APPLIED = ['Postulé', 'Réponse RH', 'Entretien RH', 'Entretien manager', 'Test/case',
               'Offre', 'Refus', 'Ghosté'];

function onEdit(e) {
  if (!e || !e.range) return;                       // lancé à la main depuis l'éditeur : rien à faire
  var sheet = e.range.getSheet();
  if (sheet.getName() !== SHEET_NAME) return;

  var lastCol = sheet.getLastColumn();
  if (lastCol < 1) return;
  var headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0].map(function (h) {
    return String(h).trim();
  });
  var statusCol = uniqueColumn_(headers, STATUS_HEADER);
  var dateCol = uniqueColumn_(headers, DATE_HEADER);
  if (!statusCol || !dateCol) return;               // en-tête manquant ou en double : ne rien écrire

  // La modification touche-t-elle la colonne Statut ?
  var firstCol = e.range.getColumn();
  var lastEditedCol = firstCol + e.range.getNumColumns() - 1;
  if (statusCol < firstCol || statusCol > lastEditedCol) return;

  var firstRow = Math.max(e.range.getRow(), 2);     // jamais la ligne d'en-tête
  var lastRow = e.range.getRow() + e.range.getNumRows() - 1;
  if (firstRow > lastRow) return;
  var count = lastRow - firstRow + 1;

  var statuses = sheet.getRange(firstRow, statusCol, count, 1).getValues();
  var dates = sheet.getRange(firstRow, dateCol, count, 1).getValues();
  var today = null;
  for (var i = 0; i < count; i++) {
    var status = String(statuses[i][0]).trim();
    var hasDate = dates[i][0] !== '' && dates[i][0] !== null;
    if (APPLIED.indexOf(status) === -1 || hasDate) continue;
    // "aaaa-mm-jj" saisi comme par l'utilisateur : Sheets le stocke comme une vraie date et
    // l'affiche avec le format de la colonne (jj/mm/aaaa).
    today = today || Utilities.formatDate(new Date(), e.source.getSpreadsheetTimeZone(), 'yyyy-MM-dd');
    sheet.getRange(firstRow + i, dateCol).setValue(today);
  }
}

/** Numéro (1-based) de la colonne dont l'en-tête est `name`, 0 s'il manque ou apparaît deux fois. */
function uniqueColumn_(headers, name) {
  var found = 0;
  for (var i = 0; i < headers.length; i++) {
    if (headers[i] === name) {
      if (found) return 0;
      found = i + 1;
    }
  }
  return found;
}
